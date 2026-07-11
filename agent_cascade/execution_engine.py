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

import copy
import json
import os
import re
import time
from typing import Any, Callable, Iterator, List, Optional, Tuple, Union
from enum import Enum, auto

from agent_cascade.agent_instance import ArgumentCachePool  # Cache pool for {USE_CACHED_ENTRY_N}
from agent_cascade.settings import (
    COMPRESSION_DEFAULT_FRACTION,
    DEFAULT_MAX_TURNS,
    LLM_MAX_RETRIES,
    LLM_RETRY_BASE_DELAY,
    LLM_RETRY_MAX_BACKOFF,
    TOKEN_ESTIMATE_CHAR_DIVISOR,
)

from agent_cascade.llm.schema import (
    ASSISTANT, FUNCTION, SYSTEM, USER, Message,
)
from agent_cascade.log import logger
from agent_cascade.tool_utils import (
    MAX_SPILL_SIZE,  # Use shared constant for consistency
    mark_tool_call_truncated,
    clear_truncation_state,
    generate_spillover_filename,
    resolve_cached_entry_refs,
    apply_cached_entry_resolutions,
)
# Import at module level for build_stream_update_from_pool (Minor #5 from review):
# Python caches module imports in sys.modules, so this is not a performance concern.
# Kept local to _create_and_run_agent only because execution_engine shouldn't have
# hard dependency on api_integration at module scope (cleaner separation of concerns).
from agent_cascade.utils.utils import extract_text_from_message, msg_field, msg_set

# M3: Import validate_message_pool from standalone utils module (Phase 2 Task 2.4)
# Moved to utils/pool_validation.py to break circular import chain with compression module
from .utils.pool_validation import validate_message_pool

from .agent_instance import AgentInstance, AgentState
from .lifecycle_manager import AgentLifecycleManager
from .compression.handler import CompressionHandler
from .tool_dispatcher import ToolDispatcher
from .stream_publisher import StreamPublisher
from .loop_detection import detect_loop as _canonical_detect_loop
from .inner_loop_detect import InnerLoopDetector, save_loop_sample
from .settings import InnerLoopSettings as _InnerLoopSettings
from .operation_manager import set_current_instance_name, clear_current_instance_name

# Sampling & limit parameters to strip when custom sampling is disabled for an endpoint.
SAMPLING_AND_LIMIT_KEYS = frozenset({
    'temperature', 'top_p', 'top_k', 'min_p',
    'repeat_penalty', 'repetition_penalty', 'repeatPenalty',
    'presence_penalty', 'frequency_penalty', 'max_tokens',
})

# ── SleepAction Enum (Phase 3.1) ───────────────────────────────────────────────
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
    # Centralized disabled_tools resolution — see agent_cascade.utils.disabled_tools
    from agent_cascade.utils.disabled_tools import resolve_disabled_tools_for_agent

    inst_name = getattr(instance, 'instance_name', 'UNKNOWN') if instance else 'NO_INSTANCE'

    # Gather inputs for the centralized resolver
    instance_override = (getattr(instance, '_generate_cfg_override', None)
                        if instance is not None else None)
    template_cfg = (getattr(template.llm, 'generate_cfg', None) or {}
                    if getattr(template, 'llm', None) is not None else {})

    # Extract disabled_tools value for logging to avoid complex f-string expressions
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
        disabled |= live_disabled

    # Defensive: template.function_map may be None for templates without tools
    func_map = getattr(template, 'function_map', None)
    if not func_map:
        logger.info(f"[{inst_name}] _get_active_functions_from_template: No function_map, returning empty list")
        return []

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
    content = msg_field(msg, 'content', '')
    if not msg_field(msg, 'function_call') and isinstance(content, str) and '<|channel>thought' in content.lower():
        match = re.search(r'^\s*<\|channel>thought\n?([\s\S]*?)(?:\n?<\|channel>|$)', content, re.IGNORECASE)
        if match:
            reasoning_text = match.group(1).strip()
            cleaned_content = re.sub(r'^\s*<\|channel>thought\n?[\s\S]*?(?:\n?<\|channel>|$)', '', content, count=1, flags=re.IGNORECASE).strip()
            msg_set(msg, 'reasoning_content', reasoning_text)
            msg_set(msg, 'content', cleaned_content)


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
    extra = msg_field(msg, 'extra')
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

    # Get active functions — single source of truth for disabled_tools resolution.
    # _get_active_functions_from_template reads instance override, template config,
    # AND live pool config for real-time tool assignment updates.
    active_functions = _get_active_functions_from_template(template, instance, pool=pool)
    disabled_tools = set(template.function_map.keys()) - {f['name'] for f in active_functions}

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

    # ── Telemetry guard helper (fixes 12x duplication) ────────────────────

    def _telemetry(self):
        """Return the telemetry collector if available, else None."""
        return getattr(self.pool, 'telemetry', None)

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
            instance._slot_release = self.pool._acquire_slot(
                instance.agent_class, instance.instance_name
            )
            logger.debug(
                f"[SLOT_ACQUIRE] {context} - instance={instance.instance_name}, "
                f"class={instance.agent_class}"
            )
        except Exception as e:
            logger.error(f"[SLOT_ACQUIRE_FAILED] {context} for {instance.instance_name}: {e}")
            raise

    # ── Unified injection helpers (atomic: append + cache sync + log) ───────

    def _append_and_log(
        self,
        instance: AgentInstance,
        msg: Message,
        *,
        lock_held: bool = False  # Caller already holds _compression_lock (RLock)
    ) -> None:
        """Append a message to conversation AND log it atomically under compression lock."""
        inst_name = instance.instance_name
        agent_class = instance.agent_class
        if not lock_held:
            with instance._compression_lock:
                instance.append_message(msg)
                self.pool.get_logger(inst_name, agent_class).log_message(msg)
        else:
            instance.append_message(msg)
            self.pool.get_logger(inst_name, agent_class).log_message(msg)

    def _append_and_log_batch(
        self,
        instance: AgentInstance,
        msgs: List[Message],
        *,
        lock_held: bool = False  # Caller already holds _compression_lock (RLock)
    ) -> None:
        """Append multiple messages to conversation AND log them atomically under compression lock."""
        inst_name = instance.instance_name
        agent_class = instance.agent_class
        if not msgs:
            return
        if not lock_held:
            with instance._compression_lock:
                instance.append_messages(msgs)
                for msg in msgs:
                    self.pool.get_logger(inst_name, agent_class).log_message(msg)
        else:
            instance.append_messages(msgs)
            for msg in msgs:
                self.pool.get_logger(inst_name, agent_class).log_message(msg)

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
        response, instance.conversation) under instance._compression_lock using the
        centralized append_message() API, ensuring no length mismatches between cached 
        lists and conversation with automatic cache invalidation.

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

        # Pre-process all items into messages to avoid calling factory() twice
        processed_messages = []
        for item in raw_data:
            msg = factory(item)
            # Handle both string and list (multimodal) content types
            if isinstance(msg.content, list):
                if not msg.content:
                    continue
            elif not msg.content.strip():
                continue
            processed_messages.append(msg)

        if not processed_messages:
            return True

        # Drain pending compression notifications into the first USER message (in-tool-response pattern)
        if self.compression_handler and processed_messages:
            first_msg = processed_messages[0]
            try:
                if isinstance(first_msg.content, str):
                    first_msg.content = self.compression_handler._drain_pending_into_user_message(instance, first_msg.content)
                    # Also drain generic tool warnings into USER messages (prepended)
                    first_msg.content = self.compression_handler._drain_tool_warnings(instance, first_msg.content, prepend=True)
                    # Also drain cache notifications into USER messages (prepended)
                    first_msg.content = self.compression_handler._drain_cache_notifications(instance, first_msg.content, prepend=True)
            except Exception:
                pass  # Don't let drain failures interfere with message injection

        with instance._compression_lock:
            for msg in processed_messages:
                # Append to response accumulator (separate list for local use)
                response.append(msg)
                self._append_and_log(instance, msg, lock_held=True)
        
        # Mark activity OUTSIDE the lock to reduce hold time (Fix #1 from reviewer)
        # Call once per message, not in a nested loop
        try:
            for _ in processed_messages:
                self.pool._mark_activity(inst_name)
            
            # ── Tail sync check after drain+inject logging (design doc §5.2 — D1 fix) ──
            if getattr(self.pool.settings, 'tail_sync_check_enabled', True):
                from agent_cascade.logger.tail_sync_check import check_and_log as _check_tail
                with instance._compression_lock:
                    conv = instance.conversation
                log_inst = self.pool.get_logger(inst_name, instance.agent_class)
                _check_tail(inst_name, conv, log_inst.log_path, context="drain_inject")
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
        
        # Capture run generation to detect if a newer execution has superseded this one.
        # When user clicks Stop then Resume, pool._run_generation is incremented and
        # the old thread exits here instead of continuing with stale state.
        # NOTE: The shared ExecutionEngine's _my_generation can be overwritten by sub-agents
        # via _create_and_run_agent(). However, pool.stopped provides defense-in-depth,
        # so even if _my_generation is clobbered, the stop signal will still be detected.
        self._my_generation = self.pool._run_generation
        
        # Clear truncation state at the start of each agent turn to prevent stale markers
        clear_truncation_state()
        instance._loop_rollback_count = 0
        
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
            # Check stopped flag before acquiring slot to prevent starting new work
            if self._is_stopped(instance.instance_name):
                return  # Exit early if stopped
            instance._slot_release = None  # Initialize for proper cleanup in finally block
            self._acquire_slot_with_logging(instance, "initial")

            # Exit if stopped after slot acquire — prevents stale slot reuse post-stop
            if self._is_stopped(instance.instance_name):
                self._release_slot(instance, instance.instance_name)
                return  # Exit generator immediately instead of continuing with stale state
        else:
            # SKIP SLOT ACQUIRE — nested agent (Security/Compressor) inherits parent's slot.
            pass
        
        try:
            # ── Phase 1: Setup ─────────────────────────────────────────────
            logger.debug(f"[TURN_START] Calling _setup_turn for {instance.instance_name}")

            # Telemetry: record turn start (non-blocking)
            if (tel := self._telemetry()) is not None:
                try:
                    template = self.pool.get_template(instance.agent_class)
                    model = getattr(getattr(template, 'llm', None), 'model', '') or ''
                    cfg = getattr(getattr(template, 'llm', None), 'generate_cfg', None) or {}
                    llm_cfg = getattr(getattr(template, 'llm', None), 'cfg', None) or {}
                    api_base = llm_cfg.get('api_base', '') or llm_cfg.get('model_server', '') or ''
                    sys_prompt = ""
                    if template:
                        try:
                            m0_msgs = instance.conversation[:1]
                            for m in m0_msgs:
                                c = msg_field(m, 'content', '')
                                if isinstance(c, str):
                                    sys_prompt = c
                                    break
                        except Exception:
                            pass
                    tools_list = None
                    if template and hasattr(template, 'function_map'):
                        tools_list = sorted(template.function_map.keys())
                    fp = tel.fingerprint_config(
                        model=model, generate_cfg=cfg, system_prompt=sys_prompt, tools=tools_list,
                        api_base=api_base,
                    )
                    desc = tel.describe_config(model=model, generate_cfg=cfg, tools=tools_list, api_base=api_base)
                    tel.record_turn_start(instance.instance_name,
                                         config_fingerprint=fp, config_description=desc)
                except Exception:
                    pass

            messages, llm_messages, response = self._setup_turn(instance)
            logger.debug(f"[TURN_DONE] Got messages={len(messages)}, llm_messages={len(llm_messages)}")
            if not messages:
                # Safety: drain any queued user messages before exiting, so they aren't lost.
                # Note: Using pool.add_message() here is safe because the engine returns immediately
                # after this block (line 676), so cached lists are never used again for this instance.
                # This prevents reintroducing the silent cache rebuild bug from Fix 1.
                inst_name = instance.instance_name
                queued = self.pool.drain_queue(inst_name)
                for item in queued:
                    msg = self._make_user_message(item)
                    # Handle both string and list (multimodal) content types
                    if isinstance(msg.content, list):
                        if not msg.content:
                            continue
                    elif not msg.content.strip():
                        continue
                    try:
                        self._append_and_log(instance, msg)
                    except Exception as e:
                        logger.error(f"Failed to append queued message for {inst_name}: {e}")
                
                # ── Tail sync check after early-exit logging (design doc §5.2 — D1 fix) ──
                if queued and getattr(self.pool.settings, 'tail_sync_check_enabled', True):
                    try:
                        from agent_cascade.logger.tail_sync_check import check_and_log as _check_tail
                        with instance._compression_lock:
                            conv = instance.conversation
                        log_inst = self.pool.get_logger(inst_name, instance.agent_class)
                        _check_tail(inst_name, conv, log_inst.log_path, context="early_exit")
                    except Exception:
                        pass  # Non-critical check
                logger.debug("early exit - %s (_setup_turn returned empty)", instance.instance_name)
                # Telemetry: record turn end for early exit (non-blocking)
                if (tel := self._telemetry()) is not None:
                    try:
                        tel.record_turn_end(inst_name)
                    except Exception:
                        pass
                return  # Manual command handled or error

            max_turns = instance.max_turns or DEFAULT_MAX_TURNS
            turns_available = max_turns
            inst_name = instance.instance_name
            turns_90pct = max(2, int(max_turns * 0.1))     # 90% threshold, min 2 to avoid collision with final turn
            turns_50pct = max(3, int(max_turns * 0.5))    # 50% mid-point warning, min 3 to avoid overlap with 90%/final

            while turns_available > 0:
                # Track current turn on instance for system_info tool access
                instance._current_turn = max_turns - turns_available + 1

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
                #logger.debug(f"[PRE_LLM_CHECK] Checking stop/halt/async/compression for {inst_name}")
                if self._pre_llm_checks(instance, messages, llm_messages, response, turns_available):
                    logger.debug(f"[PRE_LLM_CHECK] Condition met, continuing loop")
                    yield response
                    # ── Fix TODO #41 Root Cause 3: Break on stop/halt instead of continue ──
                    if self._check_stop_conditions(instance):
                        break
                    continue

                # Turn limit warnings (50%, 90%, and final turn) - one-time only via guard prefix dedup
                # Checked BEFORE decrement so that max_turns=1 agents still get the final turn warning
                # Injected into llm_messages only (same pattern as _inject_compression_warning)
                if turns_available == turns_50pct:
                    warn_msg = (
                        f"[SYSTEM WARNING: Halfway through your turn budget. "
                        f"You have {turns_available} turn(s) remaining out of {max_turns} total. "
                        f"Assess your progress and plan remaining steps.]"
                    )
                    self._append_system_notification(llm_messages, "[SYSTEM WARNING: Halfway", warn_msg)
                if turns_available == turns_90pct:
                    warn_msg = (
                        f"[SYSTEM WARNING: Turn limit approaching. "
                        f"You have {turns_available} turn(s) remaining out of {max_turns} total. "
                        f"Plan your remaining steps carefully.]"
                    )
                    self._append_system_notification(llm_messages, "[SYSTEM WARNING: Turn limit approaching", warn_msg)
                if turns_available == 1:
                    # Final turn warning: insert as a separate user message (not inline)
                    # so it's treated as a distinct conversational turn, not appended to the last message
                    final_msg = self._make_user_message(
                        f"[SYSTEM WARNING: Final turn. You have 1 turn left to complete your task. "
                        f"Wrap up and deliver your results now.]"
                    )
                    self._append_and_log(instance, final_msg)
                    llm_messages.append(final_msg)

                turns_available -= 1

                # ── Phase 3: LLM Call with Injection Points ────────────────
                #logger.debug(f"[LLM_CALL_START] Calling LLM for {inst_name} with {len(llm_messages)} messages")
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
                        content = msg_field(msg, 'content', '')
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

                # Check generation change (old run superseded by newer one) alongside stop
                if self._is_stopped(instance.instance_name):
                    logger.debug("halted/stopped/superseded - %s", instance.instance_name)
                    time.sleep(0.1)
                    yield response
                    break  # ── Fix TODO #41: Break immediately instead of continuing loop ──

                # logger.debug(f"[LLM_DONE] {inst_name} got {len(turn_output)} messages from LLM")
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
                # Inject turn limit notice into the LAST assistant message content
                # instead of appending a new message. This ensures extract_instance_output()
                # (which reads messages[-1]) returns the agent's actual output with the warning.
                turn_notice = "\n\n[Turn limit reached — results may be incomplete. Continue if needed.]"
                notice_appended = False
                if instance.conversation:
                    # Find the last assistant message with text content and append the notice
                    for msg in reversed(instance.conversation):
                        msg_role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
                        if msg_role != ASSISTANT:
                            continue
                        # Try to append to text content
                        if isinstance(msg, dict):
                            content = msg.get('content', '')
                            if isinstance(content, list):
                                for item in content:
                                    if isinstance(item, dict) and item.get('type') == 'text':
                                        item['text'] = item['text'] + turn_notice
                                        notice_appended = True
                                        break
                            elif isinstance(content, str):
                                msg['content'] = content + turn_notice
                                notice_appended = True
                        else:
                            content = getattr(msg, 'content', '')
                            if isinstance(content, list):
                                for item in content:
                                    if isinstance(item, dict) and item.get('type') == 'text':
                                        item['text'] = item['text'] + turn_notice
                                        notice_appended = True
                                        break
                            elif isinstance(content, str):
                                msg.content = content + turn_notice
                                notice_appended = True
                        if notice_appended:
                            break
                    # Fallback: if no assistant message with text found, append a new one
                    if not notice_appended:
                        msg = Message(role=ASSISTANT, content=turn_notice)
                        self._append_and_log(instance, msg)
                # Also append to response so UI streams the notice
                response_msg = Message(role=ASSISTANT, content=turn_notice)
                response.append(response_msg)

            # Telemetry: record turn end (non-blocking)
            inst_name_turn = instance.instance_name
            if (tel := self._telemetry()) is not None:
                try:
                    tel.record_turn_end(inst_name_turn)
                except Exception:
                    pass

        except Exception as e:
            # C4 fix: Catch unhandled exceptions — log and yield error state
            logger.error("EXCEPTION - %s: %s: %s", instance.instance_name, type(e).__name__, e)
            # Telemetry: record turn end on exception (non-blocking)
            if (tel := self._telemetry()) is not None:
                try:
                    tel.record_turn_end(instance.instance_name)
                except Exception:
                    pass
            error_msg = Message(role=ASSISTANT, content=f"[SYSTEM ERROR: {e}]")
            yield [error_msg]

        finally:
            # C4 fix: Always clean up — transition to IDLE regardless of how we exit
            
            # ── Fix TODO #41: Cancel async tasks and drain results on ALL exit paths (normal, exception, early return) ──
            if hasattr(self.pool, '_async_registry'):
                try:
                    self.pool._async_registry.clear_pending(instance.instance_name)
                except Exception:
                    pass  # Non-critical cleanup
            # Also drain the async results buffer to prevent memory leak from stale results
            if hasattr(self.pool, '_async_results'):
                try:
                    self.pool._async_results.drain(instance.instance_name)
                except Exception:
                    pass  # Non-critical cleanup
            
            # FIX Critical #2: Clear any pending continue merge state on exception/early exit
            with instance._compression_lock:
                if instance._continue_saved_msg is not None:
                    logger.debug(
                        f"[CONTINUE_FIX] Continue saved message not merged (merge path skipped) for {instance.instance_name}. "
                        f"Content is in conversation; this is expected on early exit."
                    )
                    instance._continue_saved_msg = None
            
            # Release concurrency slot on exit if still held (using helper method FIX Mi3)
            self._release_slot(instance, instance.instance_name)
            
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
                        conv = list(instance.conversation)
                        # Catch-all: messages are already in conversation; only JSONL needs catching up.
                        for msg in conv[already_logged_count:]:
                            if isinstance(msg, Message) or (isinstance(msg, dict) and 'role' in msg):
                                log_inst.log_message(msg)
                        
                        # ── Tail sync check after final sync (design doc §5.2 — D1 fix) ──
                        if getattr(self.pool.settings, 'tail_sync_check_enabled', True):
                            from agent_cascade.logger.tail_sync_check import check_and_log as _check_tail
                            _check_tail(inst_name, conv, log_inst.log_path, context="final_sync")
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
                    logger.debug(f"[CACHE_HIT] Reusing cached messages={len(instance._cached_messages)}, llm_messages={len(instance._cached_llm_messages)}")
                    return instance._cached_messages, instance._cached_llm_messages, []

        # Cache miss or config change - rebuild from pool (Fix 3: promoted to INFO for visibility)
        logger.info(f"[CACHE_REBUILD] Rebuilding working set for {inst_name} (conv_len={len(conv)})")

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
                    
                    # 4. Inject Argument Caching Pool instructions — all agents
                    if '### Advanced Feature: Argument Caching Pool' not in m0_content:
                        m0_content += (
                            "\n\n### Advanced Feature: Argument Caching Pool\n"
                            "The system maintains a rolling cache of tool arguments and large outputs (>1000 chars).\n"
                            "Each cached entry is assigned a sequential index N. You can insert any cached entry by using\n"
                            'the placeholder "{USE_CACHED_ENTRY_N}" inside any tool argument value, where N is the cache index.\n'
                            "A single argument value can contain multiple placeholders, e.g.\n"
                            '  content: "I found {USE_CACHED_ENTRY_12} from X and {USE_CACHED_ENTRY_23} from Y."\n'
                            "Each placeholder is independently resolved and replaced with its cached value.\n"
                            "Use system_info to view the current cache pool state. When entries are cached, you will see a [CACHE INFO] notification."
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
                            log_inst._file_history_synced = True
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
        # logger.info(f"[SETUP_TURN] messages={len(conv)}, llm_messages={len(llm_messages)}, roles={[m.get('role') if isinstance(m, dict) else getattr(m, 'role', '?') for m in llm_messages]}")
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
        return self._is_stopped(inst_name)

    def _is_stopped(self, inst_name: str) -> bool:
        """Check if pool is stopped, run superseded, or instance terminated.
        
        Centralized stop condition check used throughout execution_engine.py to avoid
        duplicated 4-condition checks across 8+ locations. Returns True immediately
        on any stop signal for fast-path efficiency.
        
        Note: Does NOT include pause — pause is handled separately via cooperative
        wait loops (e.g. `while self.pool.is_paused(): time.sleep(0.1)`).
        Pause should not interrupt execution; it should just wait and resume.
        
        Args:
            inst_name: Instance name to check halt/termination status
            
        Returns:
            True if any stop condition met, False otherwise.
        """
        return (self.pool.stopped or 
                self._my_generation != self.pool._run_generation or
                inst_name in self.pool._halted_instances or
                self.pool.is_instance_terminated(inst_name))

    def _is_stop_interrupted(self, inst_name: str) -> bool:
        """Check if pool is stopped, run superseded, or instance terminated (excludes pause).
        
        Used during streaming (Phase 3) where only actual stop/termination should interrupt.
        Pause does NOT trigger interruption — it should only block tool execution (Phase 4).
        This enforces the semantic distinction: pause = "hold tools", not "abort streaming".
        
        Args:
            inst_name: Instance name to check halt/termination status
            
        Returns:
            True if any stop condition met (excluding pause), False otherwise.
        """
        return (self.pool.stopped or 
                self._my_generation != self.pool._run_generation or
                inst_name in self.pool._halted_instances or
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
        and loop detection (inline rollback + hint if found).
        """
        inst_name = instance.instance_name
        
        # 1. Stop/halt checks
        if self._check_stop_conditions(instance):
            logger.debug(f"[PRE_LLM] Stop/halt condition met for {inst_name}")
            return True  # Skip LLM call, yield and continue loop
        
        # 2. Async message injection
        if self._inject_async_messages(instance, messages, llm_messages, response):
            return True  # Yield and continue loop to process new messages
        
        # 3. Rollback command check (delegated to compression_handler)
        # Pass response so notification messages get yielded (fixes compress feedback bug)
        if self.compression_handler.handle_rollback_command(instance, messages, llm_messages, response):
            logger.debug(f"[PRE_LLM] Rollback command handled for {inst_name}")
            return True  # Command handled — yield and continue
        
        # 4. Compress command check (Phase 4.2: delegated to compression_handler)
        # Pass response so notification messages get yielded (fixes compress feedback bug)
        if self.compression_handler.handle_compress_command(instance, messages, llm_messages, response):
            logger.debug(f"[PRE_LLM] Compress command handled for {inst_name}")
            return True  # Command handled — yield and continue
        
        # 5. Compression trigger (pass response for notification feedback)
        if self._check_and_trigger_compression(instance, messages, llm_messages, response):
            logger.debug(f"[PRE_LLM] Compression triggered for {inst_name}")
            return True  # Compression triggered — yield and continue
        
        # 6. Loop detection (with post-compression cooldown) ───────────────────
        # After compression, the conversation state has concentrated patterns that
        # can trigger false-positive loop detection. Skip detection on the turn
        # immediately following compression via _suppress_loop_detection_next_turn flag.
        # Thread safety: Python GIL ensures atomic reads/writes for simple boolean attributes.
        if not getattr(instance, '_suppress_loop_detection_next_turn', False):
            loop_info = _canonical_detect_loop(messages)
            if loop_info:
                reason, pop_count = loop_info
                logger.debug(
                    f"[LOOP_DETECTED] {inst_name}: pattern={reason}, "
                    f"pop_count={pop_count}, messages={len(messages)}"
                )

                # ── Inline rollback + hint (no exception, no retry loop) ──────
                # Track rollback count to prevent infinite recovery loops
                rollbacks = getattr(instance, '_loop_rollback_count', 0) + 1
                instance._loop_rollback_count = rollbacks

                self._inline_rollback_and_hint(
                    instance, inst_name, pop_count, reason,
                    messages, llm_messages, response,
                )

                if rollbacks >= 3:
                    logger.warning(
                        f"Loop recovery for {inst_name}: rolled back "
                        f"{rollbacks} times without success. Continuing."
                    )

                # Telemetry: record loop detection (non-blocking)
                if (tel := self._telemetry()) is not None:
                    try:
                        tel.record_loop_detected(
                            inst_name, reason=reason, auto_rolled_back=True, pop_count=pop_count,
                        )
                    except Exception:
                        pass

                return True  # Continue loop with fresh state
        else:
            # Clear the cooldown flag now that we've skipped loop detection this turn.
            # Next turn will run normal loop detection (no more suppression).
            instance._suppress_loop_detection_next_turn = False

            # Also reset rollback counter after compression (conversation state changed)
            if hasattr(instance, '_loop_rollback_count'):
                instance._loop_rollback_count = 0

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

        # Step 2: Append hint message (goes to conversation + logger atomically)
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
            'terminated', 'fetch failed',  # Connection termination patterns from logs
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

    def _ensure_image_captions(self, messages):
        """Generate captions for uncaptioned images using any vision-capable endpoint.
        
        Captions are stored as metadata on ContentItem so they survive when falling
        back to text-only endpoints. This is called before LLM calls that may route
        through non-vision endpoints.
        """
        router = getattr(self.pool, 'api_router', None)
        if router and hasattr(router, 'caption_images'):
            agent_type = 'generalist'  # Use generalist for captioning to avoid circular routing
            return router.caption_images(messages, agent_type=agent_type)
        return messages

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
        loop_retry_count = 0       # Dedicated counter for inner-loop retries (gated by pool.settings.loop_max_retries)
        error_already_yielded = False
        _loop_max = max(1, getattr(self.pool.settings, 'loop_max_retries', 2))  # At least 1 retry to avoid instant failure
        
        # Estimate input tokens for telemetry (rough char-based estimate)
        _input_tokens_est = sum(len(m.content or '') for m in llm_messages) // TOKEN_ESTIMATE_CHAR_DIVISOR

        # Ensure images have captions before any LLM call — this enables graceful fallback
        # to text-only endpoints by replacing image data with text descriptions.
        # Captions are generated ONCE and cached on ContentItem objects, so subsequent retries reuse them.
        if self._has_images(llm_messages):
            llm_messages = self._ensure_image_captions(llm_messages)

        while retry_count <= LLM_MAX_RETRIES:
            try:
                # Telemetry: record LLM call start (non-blocking)
                if (tel := self._telemetry()) is not None:
                    try:
                        model = getattr(template.llm, 'model', '') or ''
                        tel.record_llm_call_start(
                            inst_name, input_tokens_est=_input_tokens_est, model=model,
                        )
                    except Exception:
                        pass
                
                # Streaming UI Content Update Fix: Track partial LLM content for UI updates every ~100ms
                last_streaming_update_time = time.monotonic()

                # Inner-loop detector: fresh instance per retry attempt to catch generation loops mid-stream
                # Build settings from pool (UI-overridable) so per-mode toggles apply in real time
                _ps = self.pool.settings
                _inner_settings = _InnerLoopSettings(
                    default_min_chars=getattr(_ps, 'loop_min_chars', 4000),
                    score_threshold=getattr(_ps, 'loop_score_threshold', 300),
                    char_run_enabled=getattr(_ps, 'loop_char_run_enabled', True),
                    sentence_rep_enabled=getattr(_ps, 'loop_sentence_rep_enabled', True),
                    ngram_rep_enabled=getattr(_ps, 'loop_ngram_rep_enabled', True),
                    block_rep_enabled=getattr(_ps, 'loop_block_rep_enabled', True),
                    entropy_collapse_enabled=getattr(_ps, 'loop_entropy_enabled', True),
                )
                _inner_detector = InnerLoopDetector(settings=_inner_settings)
                _prev_text_len = 0  # Tracks accumulated text length for delta extraction (delta_stream=False)

                # Max-output-token guard: safety net against LLMs exceeding their token budget
                # Resolve the output token limit from generate_cfg override, template config, or default
                _max_output_tokens = 2048  # Default cap per single response
                _gen_override = getattr(instance, '_generate_cfg_override', None)
                if _gen_override and isinstance(_gen_override, dict):
                    _mt = _gen_override.get('max_tokens') or _gen_override.get('max_output_tokens') or _gen_override.get('max_input_tokens')
                    if isinstance(_mt, int) and _mt > 0:
                        _max_output_tokens = _mt
                # Also check template LLM generate_cfg as fallback
                if _max_output_tokens == 2048:
                    _llm_cfg = getattr(getattr(template, 'llm', None), 'generate_cfg', None) or {}
                    _mt = _llm_cfg.get('max_tokens') or _llm_cfg.get('max_output_tokens')
                    if isinstance(_mt, int) and _mt > 0:
                        _max_output_tokens = _mt

                _token_guard_triggered = False  # Prevent double-trigger within same iteration

                gen = self._execute_llm_call(instance, template, llm_messages, active_functions)
                _first_token_received = False  # Flag to ensure TTFT is recorded only once per call

                # --- Interrupt helper (shared by inner-loop and max-token guards) ---
                # Defined ONCE per retry attempt before the loop to avoid recreating on every iteration.
                # Counter increments happen BEFORE yield so they update even if consumer drops the iterator.
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
                    if (tel := self._telemetry()) is not None:
                        try:
                            tel.record_llm_call_end(inst_name, output_tokens_est=0)
                        except Exception:
                            pass
                    try:
                        gen.close()
                    except RuntimeError:
                        pass  # Already closed/exhausted
                    logger.debug(f"[STREAM_GUARD] {reason_msg} for {inst_name}. Retrying…")
                    # Increment counters BEFORE yield — ensures update even if consumer drops iterator mid-yield
                    nonlocal last_output, retry_count, loop_retry_count
                    last_output = None
                    retry_count += 1
                    loop_retry_count += 1
                    yield None  # Signal UI

                for output in gen:
                    last_output = output

                    # Feed delta text to inner-loop detector (extracts new content from accumulated response)
                    try:
                        _last_msg = output[-1] if output else None
                        if _last_msg is not None:
                            _content = msg_field(_last_msg, 'content', '') or ''
                            _reasoning = msg_field(_last_msg, 'reasoning_content') or ''
                            # Handle multimodal content (list of items) for both fields
                            if isinstance(_content, list):
                                _content = ' '.join(str(c) for c in _content if isinstance(c, str))
                            else:
                                _content = str(_content)
                            if isinstance(_reasoning, list):
                                _reasoning = ' '.join(str(c) for c in _reasoning if isinstance(c, str))
                            else:
                                _reasoning = str(_reasoning)
                            _total_text = _reasoning + _content
                            # Generator is append-only (delta_stream=False), so slicing by previous length gives the new delta
                            _delta_text = _total_text[_prev_text_len:]
                            _prev_text_len = len(_total_text)

                            if _delta_text:
                                # Inner-loop detection (gated by pool settings toggle)
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
                                        # Check dedicated loop retry budget before consuming LLM_MAX_RETRIES
                                        if loop_retry_count >= _loop_max:
                                            raise Exception(
                                                f"inner_loop_exhausted: retried {_loop_max} times, "
                                                f"giving up — last reason: {_ev['reason']}"
                                            )
                                        raise Exception(f"inner_loop: {_ev['reason']}")

                            # Max-output-token guard: safety net — if LLM exceeds token budget it's likely looping
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
                                    raise Exception(f"max_tokens: ~{_est_tokens} tokens")
                    except Exception as e:
                        logger.debug(f"[INNER_LOOP] Detection error for {inst_name}: {e}")
                        # Re-raise if this is an explicit inner-loop or max-tokens detection exception
                        err_str = str(e)
                        if (err_str.startswith('inner_loop:') or err_str.startswith('max_tokens:')):
                            raise
                    
                    # Telemetry: record Time To First Token (TTFT) on the first streaming chunk
                    if not _first_token_received and (tel := self._telemetry()) is not None:
                        try:
                            tel.record_llm_first_token(inst_name)
                        except Exception:
                            pass
                        _first_token_received = True
                    
                    # Check stop/halt mid-stream FIRST (before any work) — ensures fastest response to stop.
                    # Also checks generation change (old run superseded by newer one on resume).
                    # This is defense-in-depth: _check_stop_conditions() runs before the LLM call, but stop
                    # can also be triggered DURING the streaming call itself (while chunks are arriving).
                    # CRITICAL: Use _is_stop_interrupted() which excludes pause — pause should NOT abort
                    # ongoing streaming; it only blocks tool execution after streaming completes.
                    if self._is_stop_interrupted(inst_name):
                        # Telemetry: record LLM call end for mid-stream stop (non-blocking)
                        if (tel := self._telemetry()) is not None:
                            try:
                                tel.record_llm_call_end(inst_name, output_tokens_est=0)
                            except Exception:
                                pass
                        with instance._compression_lock:
                            instance._streaming_responses = []
                        # ── Fix TODO #41 Root Cause 2: Clean up async tasks during streaming stop ──
                        if hasattr(self.pool, '_async_registry'):
                            try:
                                self.pool._async_registry.clear_pending(inst_name)
                            except Exception:
                                pass  # Non-critical cleanup
                        try:
                            gen.close()  # Explicitly close generator → triggers finally blocks → releases HTTP connection + semaphore immediately
                        except RuntimeError:
                            pass  # Already closed/exhausted
                        yield None  # Signal UI that stop was detected mid-stream
                        break
                    
                    # Update _streaming_responses every ~100ms with deep copy of partial content
                    current_time = time.monotonic()
                    if current_time - last_streaming_update_time >= 0.1:
                        with instance._compression_lock:
                            self._update_streaming_responses(instance, last_output)
                            last_streaming_update_time = current_time
                    
                    # Re-check stop/halt after UI update (defense in depth — catches stop during slow streaming)
                    # CRITICAL: Use _is_stop_interrupted() which excludes pause — same as above.
                    if self._is_stop_interrupted(inst_name):
                        # Telemetry: record LLM call end for mid-stream stop (non-blocking)
                        if (tel := self._telemetry()) is not None:
                            try:
                                tel.record_llm_call_end(inst_name, output_tokens_est=0)
                            except Exception:
                                pass
                        with instance._compression_lock:
                            instance._streaming_responses = []
                        # ── Fix TODO #41 Root Cause 2: Clean up async tasks during streaming stop ──
                        if hasattr(self.pool, '_async_registry'):
                            try:
                                self.pool._async_registry.clear_pending(inst_name)
                            except Exception:
                                pass  # Non-critical cleanup
                        try:
                            gen.close()  # Explicitly close generator → triggers finally blocks → releases HTTP connection + semaphore immediately
                        except RuntimeError:
                            pass  # Already closed/exhausted
                        yield None  # Signal UI that stop was detected mid-stream
                        break
                    
                    # Yield partial content for UI update (after both checks pass)
                    yield None
                
                if last_output is not None:
                    # Telemetry: record LLM call end for successful completion (non-blocking)
                    # Bug #45 fix: prefer actual API-reported completion_tokens over char-based estimate
                    _output_tokens_est = 0
                    _found_usage = False
                    
                    # Try to get actual completion_tokens from first message with usage data
                    # (multiple messages in same chunk share usage, so assign not sum)
                    for m in last_output:
                        extra = getattr(m, 'extra', None) or {}
                        usage = extra.get('usage') if isinstance(extra, dict) else None
                        if usage and isinstance(usage, dict) and 'completion_tokens' in usage:
                            ct = usage['completion_tokens']
                            if isinstance(ct, (int, float)) and ct > 0:
                                _output_tokens_est = int(ct)
                                _found_usage = True
                            break  # Only need first valid usage entry
                    
                    # Try completion_tokens_details as alternative if completion_tokens not available
                    if not _found_usage:
                        for m in last_output:
                            extra = getattr(m, 'extra', None) or {}
                            usage = extra.get('usage') if isinstance(extra, dict) else None
                            if usage and isinstance(usage, dict):
                                details = usage.get('completion_tokens_details', {})
                                if details:
                                    reasoning_t = details.get('reasoning_tokens', 0) or 0
                                    tool_t = details.get('tool_calls_tokens', 0) or 0
                                    accepted_pred_t = details.get('accepted_prediction_tokens', 0) or 0
                                    audio_t = details.get('audio_tokens', 0) or 0
                                    total_from_details = reasoning_t + tool_t + accepted_pred_t + audio_t
                                    if total_from_details > 0:
                                        _output_tokens_est = total_from_details
                                        _found_usage = True
                                        break
                    
                    if not _found_usage:
                        # Fallback: char-based estimate including reasoning_content and function_call arguments
                        for m in last_output:
                            c = getattr(m, 'content', '') or ''
                            rc = getattr(m, 'reasoning_content', '') or ''
                            fc = getattr(m, 'function_call', None)
                            # Normalize list content to string (multimodal messages)
                            if isinstance(c, list):
                                c = ' '.join(str(x) for x in c if isinstance(x, str))
                            else:
                                c = str(c)
                            if isinstance(rc, list):
                                rc = ' '.join(str(x) for x in rc if isinstance(x, str))
                            else:
                                rc = str(rc)
                            # Include function call arguments in token estimate
                            fc_str = ''
                            if fc:
                                if isinstance(fc, dict):
                                    fc_str = str(fc.get('name', '')) + str(fc.get('arguments', ''))
                                elif hasattr(fc, 'name'):
                                    fc_str = str(getattr(fc, 'name', '')) + str(getattr(fc, 'arguments', ''))
                            _output_tokens_est += (len(c) + len(rc) + len(fc_str)) // TOKEN_ESTIMATE_CHAR_DIVISOR
                    
                    if (tel := self._telemetry()) is not None:
                        try:
                            tel.record_llm_call_end(inst_name, output_tokens_est=_output_tokens_est)
                        except Exception:
                            pass
                    break
                
                # Telemetry: record LLM call end for empty response before retrying (non-blocking)
                if (tel := self._telemetry()) is not None:
                    try:
                        tel.record_llm_call_end(inst_name, output_tokens_est=0)
                    except Exception:
                        pass
                
            except Exception as e:
                with instance._compression_lock:
                    instance._streaming_responses = []
                
                if retry_count > LLM_MAX_RETRIES:
                    # Telemetry: record LLM call end for exhausted retries (non-blocking)
                    if (tel := self._telemetry()) is not None:
                        try:
                            tel.record_llm_call_end(inst_name, output_tokens_est=0)
                        except Exception:
                            pass
                    error_msg = str(e).split('\n')[0] if e else "Unknown error"
                    # Give clearer message for loop detection failures
                    if 'inner_loop_exhausted' in error_msg:
                        display_msg = f"LLM generation loop detected (exceeded {_loop_max} loop retries)"
                    elif 'inner_loop' in error_msg:
                        display_msg = f"LLM generation loop detected (tried {LLM_MAX_RETRIES} times)"
                    elif 'max_tokens' in error_msg:
                        display_msg = f"LLM exceeded token limit (tried {LLM_MAX_RETRIES} times)"
                    else:
                        display_msg = f"LLM call failed after {LLM_MAX_RETRIES} retries — {error_msg}"
                    logger.error(f"[ENDPOINT_RETRY] LLM call failed for {inst_name} after {LLM_MAX_RETRIES} retries: {e}")
                    yield Message(role=ASSISTANT, content=f"[SYSTEM ERROR: {display_msg}]")
                    error_already_yielded = True
                    break
                
                # _abort_stream already increments retry_count before raising.
                # For regular exceptions, increment if we haven't yet (retry_count==0).
                if retry_count == 0:
                    retry_count += 1
                else:
                    # Already incremented by _abort_stream OR previous round — check if
                    # we need another increment (only if _abort_stream didn't do it).
                    # We know _abort_stream incremented if error message contains our markers.
                    if 'inner_loop' not in str(e) and 'max_tokens' not in str(e):
                        retry_count += 1
                    
                    # Check dedicated loop retry budget — fail fast before consuming LLM_MAX_RETRIES
                    error_str = str(e)
                    if ('inner_loop' in error_str) and loop_retry_count >= _loop_max:
                        raise Exception(
                            f"inner_loop_exhausted: retried {_loop_max} times, "
                            f"giving up — last reason: {error_str.split(':')[-1].strip()}"
                        )

                # Classify error type
                error_type = self._classify_llm_error(e)
                
                # Telemetry: record LLM call end for failed retry attempt before continuing (non-blocking)
                # Skip for fatal errors — they have their own end call below
                if error_type != 'fatal' and (tel := self._telemetry()) is not None:
                    try:
                        tel.record_llm_call_end(inst_name, output_tokens_est=0)
                    except Exception:
                        pass
                
                if error_type == 'fatal':
                    # Telemetry: record LLM call end for fatal error (non-blocking)
                    if (tel := self._telemetry()) is not None:
                        try:
                            tel.record_llm_call_end(inst_name, output_tokens_est=0)
                        except Exception:
                            pass
                    error_msg = str(e).split('\n')[0] if e else "Unknown error"
                    logger.warning(f"[ENDPOINT_RETRY] LLM call failed for {inst_name} with non-retryable error: {e}")
                    yield Message(role=ASSISTANT, content=f"[SYSTEM ERROR: LLM call failed — {error_msg}]")
                    error_already_yielded = True
                    break
                
                logger.warning(
                    f"[ENDPOINT_RETRY] LLM call failed for {inst_name}, retry {retry_count}/{LLM_MAX_RETRIES}. "
                    f"Retrying in {backoff:.1f}s with new endpoint... Error: {e}"
                )
                
                # Signal retry to UI before blocking on sleep
                yield self._make_retrying_message(instance, retry_count, LLM_MAX_RETRIES, backoff)
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
            
            # Derive agent type once for the entire method (used by token resolution + router call)
            agent_type = instance.agent_class.lower() if hasattr(instance, 'agent_class') else 'agent'

            # Dynamic endpoint selection based on agent's actual token requirements
            allocated_tokens = None
            override = getattr(instance, '_generate_cfg_override', None)
            if override is not None and 'max_input_tokens' in override:
                val = override['max_input_tokens']
                if isinstance(val, int) and val > 0:
                    allocated_tokens = val
            
            # Prefer live API router data over stale template config (fixes max_tokens not updating on live config changes)
            if allocated_tokens is None:
                try:
                    val = self.pool.api_router.get_effective_max_tokens(agent_type)
                    if isinstance(val, int) and val > 0:
                        allocated_tokens = val
                        
                        # Log endpoint allocation with stats for observability
                        prev_tokens = getattr(instance, '_allocated_max_input_tokens', 0)
                        
                        # Try to get the active endpoint details from priority list
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
            
            # Template config as last resort (only used if no override and router unavailable/empty)
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
                #   2. Per-instance override           – user-specified values via UI
                #   3. Endpoint config from fallback chain – sampler params (when use_custom_sampling=True) win
                merged_cfg = self._build_merged_cfg(llm, instance, endpoint_cfg=llm_cfg)
                merged_cfg['agent_name'] = template.name
                
                # Store allocated max_input_tokens in instance for compression check (ground-truth tracking)
                self._store_allocated_max_input_tokens(instance, merged_cfg)

                # Register token count callback to capture actual token usage from LLM (ground-truth tracking)
                merged_cfg['_on_token_count'] = _make_token_count_callback(instance)

                return llm.chat(
                    messages=messages,
                    functions=active_functions,
                    stream=True,
                    delta_stream=False,
                    extra_generate_cfg=merged_cfg,
                )

            # Pass _do_call directly — call_with_fallback handles generator lifecycle via finally blocks
            return self.pool.api_router.call_with_fallback(
                agent_type, _do_call, allocated_tokens=allocated_tokens
            )
        else:
            # Direct call without router — same merge priority as fallback path:
            merged_cfg = self._build_merged_cfg(llm, instance)  # no endpoint config layer in direct call
            merged_cfg['agent_name'] = template.name
            
            # Store allocated max_input_tokens in instance for compression check (ground-truth tracking)
            self._store_allocated_max_input_tokens(instance, merged_cfg)

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
            reasoning_content = msg_field(msg, 'reasoning_content')
            if isinstance(reasoning_content, str):
                msg_set(msg, 'reasoning_content', _normalize_thinking_blocks(reasoning_content))
            
            # Clean thinking blocks from function call arguments (P4 continuation)
            func_call = msg_field(msg, 'function_call')
            if func_call:
                _normalize_tool_arguments(func_call)

    def _log_messages_to_jsonl(
        self,
        instance: AgentInstance,
        inst_name: str,
        turn_output: List[Message]
    ) -> None:
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

        already_logged_count = len(log_inst.data.get("history", []))
        with instance._compression_lock:
            conv = instance.conversation
        
        # Log delta: any messages in conv that aren't yet in the logger history.
        # This covers ALL message types uniformly (system, user, assistant, function).
        # turn_output is already in conv by the time this runs (appended before logging),
        # so it's naturally included in the delta — no separate loop needed.
        try:
            wrote_any = False
            if already_logged_count < len(conv):
                for msg in conv[already_logged_count:]:
                    # Check both text content and function_call to avoid skipping
                    # assistant messages that have tool calls but empty content.
                    # Skipping such messages breaks the count-based delta sync,
                    # causing duplicate entries on the next logging pass.
                    has_content = bool(str(msg_field(msg, 'content', '')).strip())
                    has_function_call = bool(msg_field(msg, 'function_call'))
                    if has_content or has_function_call:
                        log_inst.log_message(msg)
                        wrote_any = True
            
            # ── Tail sync check after write (design doc §5.2 — D1 fix) ──
            # Lightweight length-only verification that pool tail matches JSONL tail.
            if wrote_any and getattr(self.pool.settings, 'tail_sync_check_enabled', True):
                from agent_cascade.logger.tail_sync_check import check_and_log as _check_tail
                _check_tail(inst_name, conv, log_inst.log_path, context="log_messages")
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
        if is_truncated and not self._is_stopped(inst_name) and self.pool.settings.auto_continue:
            logger.info(f"Detected message truncation for {inst_name}. Auto-continuing.")
            cont_msg = Message(
                role=USER,
                content="[SYSTEM]: Your previous response was cut off. Continue from where you left off."
            )
            self._append_and_log(instance, cont_msg)
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
        # Centralized disabled_tools resolution — see agent_cascade.utils.disabled_tools
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
                self.pool.wait_if_paused(timeout=1.0)
            
            # Stop/halt check BEFORE tool execution (check before setting used_any_tool)
            if self._is_stopped(inst_name):
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
                
                used_any_tool = True
                continue  # Skip actual tool execution
            
            used_any_tool = True

            # Track tool success/failure — needed for function_id matching and frontend isToolFailure()
            _tool_success = True
            _tool_error = ""

            # Telemetry: record tool call start (non-blocking)
            if (tel := self._telemetry()) is not None:
                try:
                    tel.record_tool_call_start(inst_name, tool_name)
                except Exception:
                    pass

            # Extract function_id from the assistant message that had the tool call BEFORE executing
            # This is critical — without it, the LLM API can't match tool results to tool calls
            extra_data = out.get('extra', {}) if isinstance(out, dict) else (getattr(out, 'extra', None) or {})
            function_id = extra_data.get('function_id')

            try:
                # Set current instance name in thread-local for _resolve_path warnings
                set_current_instance_name(inst_name)

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
                    # Truncate if needed — track whether truncation actually occurred.
                # Non-string tool results bypass truncation and always report truncated=False.
                _was_truncated = False
                if isinstance(tool_result, str):
                    # Cache full output BEFORE truncation (if exceeds threshold)
                    self._cache_tool_output(
                        inst_name, tool_name, tool_result,
                        threshold=self.pool.settings.cache_threshold_chars
                    )

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
                            truncated=_was_truncated,
                            error=_tool_error,
                            is_call_agent=(tool_name == 'call_agent'),
                        )
                    except Exception:
                        pass

                # Drain pending compression notifications and tool warnings (always runs even on exceptions)
                # Only drain when tool_result is defined to avoid errors from early failures
                if self.compression_handler:
                    try:
                        tool_result = self.compression_handler._drain_pending_into_tool_result(instance, tool_result)
                        tool_result = self.compression_handler._drain_tool_warnings(instance, tool_result)
                        tool_result = self.compression_handler._drain_cache_notifications(instance, tool_result)
                    except Exception:
                        pass  # Don't let drain failures interfere with normal flow

                # Clear thread-local instance name after draining to prevent stale references across concurrent calls
                clear_current_instance_name()

            # Track compress_context execution and record telemetry
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
            self._append_and_log(instance, fn_msg)
            response.append(fn_msg)  # Stream tool result to UI (separate list for streaming)
            
            # Track executed tool for orphan handling
            executed_tools.append(tool_name)

        # ── Handle orphaned tool calls from early break ───────────────────────────────
        # If halt/stop was detected mid-loop, remaining tools in turn_output don't have FUNCTION results.
        # Add placeholder FUNCTION messages to prevent API Error 400 (orphaned tool_call_id's).
        if self._is_stopped(inst_name):
            executed_set = set(executed_tools)  # Convert to set for O(1) lookup
            tools_processed = 0
            
            # ── Hoist template lookup outside compression lock ──────────────────
            # Template and disabled tool list don't change during the loop.
            # Centralized disabled_tools resolution — see agent_cascade.utils.disabled_tools
            from agent_cascade.utils.disabled_tools import resolve_disabled_tools_for_agent

            _orphan_template = self.pool.get_template(instance.agent_class)
            _orphan_disabled_tools: set[str] = set()
            _orphan_function_map: dict = {}
            if _orphan_template:
                # Use the centralized resolver instead of duplicating inline logic
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
                    
                    # Track as executed for consistency (matching primary loop pattern)
                    executed_tools.append(tool_name)
                    
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

        self._append_and_log_batch(instance, turn_output)
        response.extend(turn_output)  # Separate list for streaming/accumulation
        # Streaming UI Content Update Fix: Clear _streaming_responses after Phase 4 commits messages
        instance._streaming_responses = []
        
        # FIX: Option B - Merge continue-saved assistant message if present.
        # When Continue is clicked, the last assistant message was popped from conversation 
        # in api_server.py continue handler and stored as _continue_saved_msg. We now merge
        # it with the newly generated assistant message to create a single concatenated message.
        
        # FIX Minor #5: Fast-check before lock acquisition to avoid unnecessary lock overhead
        if instance._continue_saved_msg is not None:
            with instance._compression_lock:
                saved = instance._continue_saved_msg
                # Clear immediately under lock to prevent another thread from setting it again
                instance._continue_saved_msg = None
            
            if saved:
                # Find the last assistant message in turn_output (most recent generation) and merge content
                merged = False
                for msg in reversed(turn_output):
                    role = msg_field(msg, 'role', '')
                    if role == ASSISTANT:
                        old_content = msg_field(saved, 'content', '') or ''
                        new_content = msg_field(msg, 'content', '') or ''
                        merged_content = old_content + new_content
                        
                        # Update the message with merged content (handle both dict and Message object)
                        msg_set(msg, 'content', merged_content)
                        
                        logger.debug(f"[CONTINUE_FIX] Merged continue-saved assistant message ({len(old_content)} chars) with new response ({len(new_content)} chars)")
                        merged = True
                        break
                
                if not merged:
                    # Fallback: saved message was popped from conversation by continue handler.
                    # If we can't merge it, re-append it to prevent data loss.
                    logger.warning(
                        f"[CONTINUE_FIX] Could not merge continue-saved message for {inst_name}: "
                        f"no assistant message found in turn_output. Re-appending as separate message."
                    )
                    self._append_and_log(instance, saved)
        
        # Extract ground-truth usage info from LLM response (ground-truth token tracking)
        # This replaces manual token counting with actual API-reported values
        for msg in turn_output:
            extra = msg_field(msg, 'extra')
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

        # Log ALL messages (turn_output + fn_msgs from tools) in a single delta pass.
        # Called AFTER tool execution so FUNCTION results are already in conv and get
        # picked up by the count-based delta sync — no risk of duplicate logging.
        self._log_messages_to_jsonl(instance, inst_name, turn_output)

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
            role = msg_field(msg, 'role', '')
            if role == ASSISTANT:
                fc = msg_field(msg, 'function_call')
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

        # Check stop immediately after LLM response — prevents unnecessary post-turn processing
        if self._is_stopped(inst_name):
            return False  # Stop detected — break from loop

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
        
        Thread-safe: acquires instance._state_lock before checking/clearing _slot_release
        to prevent double-release with concurrent stop_session calls.
        
        Args:
            slot_holder: Object with _slot_release attribute (AgentInstance or similar)
            holder_name: Name of the holder for logging purposes
            context: Optional context description for logging (e.g., "sleep transition", "sync child")
        """
        # Defensive guard: handle objects without _slot_release attribute
        if not hasattr(slot_holder, '_slot_release'):
            return
        
        context_suffix = f" during {context}" if context else ""
        # Acquire state lock for atomic check-nullify-release
        if hasattr(slot_holder, '_state_lock'):
            with slot_holder._state_lock:
                if slot_holder._slot_release is not None:
                    release_callback = slot_holder._slot_release
                    slot_holder._slot_release = None
                    try:
                        release_callback()
                    except Exception as e:
                        logger.error(
                            f"[SLOT_RELEASE_ERROR] Failed to release slot for {holder_name}{context_suffix}: {e}",
                            exc_info=True
                        )

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

        # Check stop immediately — a SLEEPING agent should not wait up to 300s for wakeup
        if self._is_stopped(inst_name):
            return SleepAction.BREAK_LOOP, None

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

                # Exit if stopped after re-acquiring slot in sleep loop
                if self._is_stopped(inst_name):
                    logger.debug(
                        f"[SLOT_STOP_CHECK] Stale slot detected after async wakeup for {inst_name}, exiting"
                    )
                    return SleepAction.BREAK_LOOP, None  # Stop detected — slot released in finally

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

                    # Exit if stopped after re-acquiring slot in sleep loop
                    if self._is_stopped(inst_name):
                        logger.debug(
                            f"[SLOT_STOP_CHECK] Stale slot detected after stable drain for {inst_name}, exiting"
                        )
                        return SleepAction.BREAK_LOOP, None  # Stop detected — slot released in finally

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

    def _ensure_cache_pool(self, instance_name: str) -> None:
        """Lazily initialize the cache pool for an instance if not yet created.

        Instances are single-threaded in practice, so a simple check suffices.

        Args:
            instance_name: The agent instance name.
        """
        inst = self.pool.get_instance(instance_name)
        if inst is None or inst.cache_pool is not None:
            return
        try:
            inst.cache_pool = ArgumentCachePool(
                max_size=self.pool.settings.cache_pool_size,
            )
            inst.cache_pool.enabled = self.pool.settings.cache_pool_enabled
        except Exception as e:
            logger.warning(f"Failed to initialize cache pool for '{instance_name}': {e}")

    def _cache_tool_args(self, instance_name: str, tool_name: str, tool_args: Any) -> None:
        """Store resolved tool arguments in the rolling cache pool for {USE_CACHED_ENTRY_N} reuse.

        Args are deep-copied to prevent later mutation of cached values.

        Args:
            instance_name: The agent instance name (scope key).
            tool_name: Name of the tool whose args are being cached.
            tool_args: Resolved arguments (after placeholder substitution).
        """
        if not isinstance(tool_args, dict):
            return  # Nothing to cache for non-dict args

        # ── Add to rolling cache pool ───────────────────────────────────────
        self._ensure_cache_pool(instance_name)
        inst = self.pool.get_instance(instance_name)
        if inst is None or inst.cache_pool is None:
            return

        cp = inst.cache_pool
        if not cp.enabled:
            return

        threshold = self.pool.settings.cache_threshold_chars

        # Cache individual arg values that pass the threshold
        cache_refs = {}
        for key, val in tool_args.items():
            if isinstance(val, str) and len(val) > threshold:
                try:
                    idx = cp.add("arg", f"{tool_name}.{key}", val, threshold=threshold)
                    cache_refs[key] = idx
                except (TypeError, AttributeError):
                    pass

        # Build notification only if something was actually cached
        if cache_refs:
            refs_str = ", ".join(
                f'"{k}" → N={n}' for k, n in cache_refs.items()
            )
            with inst._compression_lock:
                inst._cache_notifications.append(
                    f'[{tool_name}] Cached: {refs_str}'
                )

    def _cache_tool_output(self, instance_name: str, tool_name: str,
                           output: str, threshold: int = 1000) -> None:
        """Cache tool output in the rolling pool if it exceeds the threshold.

        Called BEFORE truncation so the full content is preserved.

        Args:
            instance_name: Agent instance name (scope key).
            tool_name: Name of the tool that produced this output.
            output: The tool result string (full, pre-truncation).
            threshold: Minimum character count to trigger caching.
        """
        if not isinstance(output, str) or len(output) <= threshold:
            return

        self._ensure_cache_pool(instance_name)
        inst = self.pool.get_instance(instance_name)
        if inst is None or inst.cache_pool is None:
            return

        cp = inst.cache_pool
        if not cp.enabled:
            return

        char_count = len(output)
        try:
            idx = cp.add("output", tool_name, output, threshold=threshold)
            with inst._compression_lock:
                inst._cache_notifications.append(
                    f'[{tool_name}] Output cached: N={idx} ({char_count} chars)'
                )
        except (TypeError, AttributeError):
            pass

    def _resolve_placeholders(self, tool_args: Any, instance_name: str,
                              tool_name: str) -> Optional[dict]:
        """Resolve {USE_CACHED_ENTRY_N} placeholders in tool arguments.

        If *tool_args* is a JSON string it is parsed first, then resolved.
        Resolution looks up cached entries from the rolling cache pool.
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

        # Scan for {USE_CACHED_ENTRY_N} patterns using shared function (avoids regex recompilation + code duplication)
        inst = self.pool.get_instance(instance_name)
        cache_pool = getattr(inst, 'cache_pool', None) if inst else None
        cached_refs = resolve_cached_entry_refs(parsed, cache_pool)

        # Always deep-copy for consistency (same path whether placeholders exist or not)
        resolved_args = copy.deepcopy(parsed)

        if not cached_refs:
            return resolved_args  # Nothing to resolve, but return a safe copy

        # ── Resolve {USE_CACHED_ENTRY_N} using shared function ──────────────
        apply_cached_entry_resolutions(resolved_args, cached_refs)

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
        
        # BUG FIX (Bug 2): Extract log_file from args and pass through the chain
        log_file = args.get('log_file')

        # Phase 4.1: Delegate to lifecycle manager for instance creation/reuse
        inst, is_reuse, session_was_loaded = self.lifecycle.find_or_create_instance(
            agent_class, instance_name, caller, nest_depth, force_fresh, log_file=log_file
        )

        # Phase 4.1: Delegate to lifecycle manager for system message building
        # Use inst.agent_class (may differ from caller's agent_class if session was loaded from log file)
        sys_msg = self.lifecycle.build_system_message(inst.agent_class, instance_name)

        # Phase 4.1: Delegate to lifecycle manager for task message building
        task_msg = self.lifecycle.build_task_message(args, caller)

        # Phase 4.1: Delegate to lifecycle manager for conversation initialization
        conv = self.lifecycle.initialize_conversation(
            inst, sys_msg, task_msg, is_reuse, instance_name, inst.agent_class, from_external_load=session_was_loaded
        )

        # Phase 4.1: Delegate to lifecycle manager for settings propagation
        self.lifecycle.propagate_settings(inst, caller, inst.agent_class, call_agent_args=args)

        # Track in active stack with depth info (thread-safe via RLock)
        with self.pool._execution._state_lock:
            self.pool._execution.active_stack.append((instance_name, inst._nest_depth))

        # Item 12: Initialize sub-agent WebUI state before execution begins (Fix #3: lighter snapshot)
        # Issue Y2: Use shared helper method instead of duplicated logic
        self._update_webui_state(instance_name, inst.agent_class, inst, conv, final_resp=[], is_initial=True)

        # Phase 4.4: Delegate to StreamPublisher for WebSocket push
        self.stream_publisher.push_initial_state(inst, caller)

        try:
            # Telemetry: track sub-agent call latency (non-blocking)
            _call_start = time.perf_counter()

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
                if self._is_stopped(instance_name):
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
                    self._update_webui_state(instance_name, inst.agent_class, inst, current_conv, final_resp)

                # ── Push stream_update to frontend during sub-agent execution ──
                # This is the key fix: without this, the main agent's streaming loop
                # is blocked and no WebSocket events reach the frontend. The frontend
                # relies on stream_update to call renderSubAgents() every ~200ms.
                now = time.time()  # Use time.time() for consistency with run_agent_unified.py:135
                if now - _last_sub_send >= _sub_send_interval:
                    self.stream_publisher.push_periodic_update(caller)
                    _last_sub_send = now

            # FIX MSG_COUNT_BUG: Removed conv.extend(final_resp) to prevent duplicate messages.
            # Messages are already added to instance.conversation during engine.run() via _process_response().
            # Note: For new instances, rebuild_conversation() creates a copy of conv, so they are NOT the same
            # reference — only reused instances share the same list. Extending again would cause duplication
            # regardless. See: .agent_lessons/lessons_msg_count_bug.md for detailed analysis.
            self._create_completed = True  # Mark for finally-block EXIT log reason tracking

            # Item 12: Always emit final sub-agent state after loop completes (Fix #3: lighter snapshot)
            # Ensures even short-lived agents (<5 turns) appear in the WebUI
            # Issue Y2: Use shared helper method instead of duplicated logic
            current_conv = list(inst.conversation) if hasattr(inst, 'conversation') else conv
            self._update_webui_state(instance_name, inst.agent_class, inst, current_conv, final_resp)

            # ── Push final stream_update after sub-agent completes ──
            self.stream_publisher.push_final_state(inst, caller)

        finally:
            # Telemetry: record agent instance call (non-blocking, fires in finally so failed delegations are counted too)
            _call_latency_ms = (time.perf_counter() - _call_start) * 1000
            if (tel := self._telemetry()) is not None:
                try:
                    tel.record_agent_instance_call(
                        instance_name, agent_class, caller, latency_ms=_call_latency_ms,
                    )
                except Exception:
                    pass

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

        # FIX: Return a copy of the actual instance conversation, not the stale `conv` variable.
        # For new instances, rebuild_conversation() creates a COPY of conv via list(new_messages),
        # so the original `conv` from initialize_conversation never receives appended messages.
        # For reused instances, they happen to be the same reference — but using inst.conversation
        # is correct in both cases. See: investigation report for sub-agent response propagation bug.
        return inst, list(inst.conversation)

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
        inst, is_reuse, session_was_loaded = self.lifecycle.find_or_create_instance(
            agent_class, instance_name, caller, nest_depth=0, force_fresh=True
        )
        
        # Build system message using lifecycle manager (use inst.agent_class for consistency)
        sys_msg = self.lifecycle.build_system_message(inst.agent_class, instance_name)
        
        # Build task message using lifecycle manager
        task_msg = self.lifecycle.build_task_message(args, caller)
        
        # Initialize conversation using lifecycle manager (pass actual is_reuse value)
        conv = self.lifecycle.initialize_conversation(
            inst, sys_msg, task_msg, is_reuse=is_reuse, instance_name=instance_name, agent_class=inst.agent_class, from_external_load=session_was_loaded
        )
        
        # Phase 4.1: Propagate settings from caller to system agent
        self.lifecycle.propagate_settings(inst, caller, inst.agent_class)
        
        # Track in active stack (thread-safe)
        self.pool.active_stack_append(instance_name, 0)
        
        # Initialize WebUI state for immediate tab visibility
        # Issue Y2: Use shared helper method instead of duplicated logic
        self._update_webui_state(instance_name, inst.agent_class, inst, conv, final_resp=[], is_initial=True)
        
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
                role = msg_field(last_msg, 'role', '')
                content = msg_field(last_msg, 'content', '')
                # For FUNCTION role (tool results), include tool name in summary
                if role == FUNCTION:
                    from agent_cascade.utils.utils import format_tool_result_preview
                    tool_name = msg_field(last_msg, 'name', '')
                    latest_summary = format_tool_result_preview(tool_name, content, max_len=450)
                else:
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
                
                role = msg_field(msg, 'role', '')
                func_call = msg_field(msg, 'function_call')

                # For assistant with function call, count the function call string
                # plus any reasoning_content that might accompany it
                if role == ASSISTANT and func_call:
                    total += qwen_count(f'{func_call}')
                    # Also count reasoning_content for function call messages
                    rc = msg_field(msg, 'reasoning_content') or msg_field(msg, 'reasoning')
                    if rc:
                        rc_str = str(rc).strip() if not isinstance(rc, list) else ' '.join(
                            (item.get('text', '') if isinstance(item, dict) else getattr(item, 'text', ''))
                            for item in rc if (item.get('text', '') if isinstance(item, dict) else getattr(item, 'text', None))
                        )
                        total += qwen_count(rc_str)
                    continue

                msg_obj = Message(**msg) if isinstance(msg, dict) else msg
                text = extract_text_from_message(msg_obj, add_upload_info=True)
                
                # Count reasoning_content separately to avoid undercounting.
                # extract_text_from_message only includes reasoning as fallback when
                # content is empty, so we always count it explicitly here.
                rc = msg_field(msg_obj, 'reasoning_content') or msg_field(msg_obj, 'reasoning')
                if rc:
                    if isinstance(rc, list):
                        rc_texts = []
                        for item in rc:
                            t = item.get('text', '') if isinstance(item, dict) else getattr(item, 'text', '')
                            if t:
                                rc_texts.append(str(t))
                        rc_str = ' '.join(rc_texts)
                    else:
                        rc_str = str(rc).strip()
                else:
                    rc_str = ''

                total += qwen_count(text)

                # Add reasoning content tokens, but skip if main content is empty
                # (extract_text already included reasoning as fallback in that case).
                if text and rc_str:
                    total += qwen_count(rc_str)

            # Update cache
            if inst:
                inst._cached_token_count = total
                inst._last_token_count_conversation_length = len(messages)

            return total
        except Exception as e:
            logger.debug(f"Token counting failed (using rough estimate): {e}")
            # Fallback: rough estimate (4 chars per token), including reasoning_content
            total_chars = 0
            for m in messages:
                if isinstance(m, list):
                    continue
                content = m.get('content', '') if isinstance(m, dict) else getattr(m, 'content', '')
                total_chars += len(str(content or ''))
                # Also count reasoning_content to avoid undercounting
                rc = m.get('reasoning_content') if isinstance(m, dict) else getattr(m, 'reasoning_content', None)
                if rc:
                    if isinstance(rc, list):
                        for item in rc:
                            txt = item.get('text', '') if isinstance(item, dict) else getattr(item, 'text', '')
                            total_chars += len(str(txt or ''))
                    else:
                        total_chars += len(str(rc))
            return max(total_chars // TOKEN_ESTIMATE_CHAR_DIVISOR, 100)

    # ── _detect_loop removed — now uses canonical detect_loop from loop_detection.py ──

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
        content = msg_field(last_msg, 'content')

        if isinstance(content, str):
            if guard_prefix not in content:
                new_content = content + f"\n\n{notification_text}"
                msg_set(last_msg, 'content', new_content)
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