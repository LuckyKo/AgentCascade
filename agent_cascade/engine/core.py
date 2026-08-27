"""ExecutionEngine — the composed engine class (Phase 1 module-split).

``ExecutionEngine`` is defined here as the composition of three mixins that hold
the method clusters moved out of the original monolithic ``execution_engine.py``:

- :class:`~agent_cascade.engine.llm_call.LLMCallMixin`         — LLM-call cluster
- :class:`~agent_cascade.engine.compression_exec.CompressionExecMixin` — compression
- :class:`~agent_cascade.engine.tool_execution.ToolExecMixin`  — tool execution / images

``__init__``, ``initialize`` and the core orchestration / state / streaming / slot
methods remain in this class. All method bodies are moved VERBATIM from the
original file; only the mixin composition, module constants, and imports were added.
"""

from __future__ import annotations

import copy
import json
import random
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator, List, Optional, Tuple, Union

from agent_cascade.agent_instance import ArgumentCachePool
from agent_cascade.settings import (
    AGENT_SLEEPING_MAX_WAIT_SECONDS,
    AUTO_SKILL_ENABLED,
    AUTO_SKILL_EXTRA_TURNS,
    CHARS_PER_TOKEN_ESTIMATE,
    COMPRESSION_DEFAULT_FRACTION,
    COMPRESSION_RECOUNT_THRESHOLD,
    DEFAULT_LOAD_SKILL_MODE, LOAD_SKILL_NONE, LOAD_SKILL_AUTO,
    DEFAULT_MAX_INPUT_TOKENS,
    DEFAULT_MAX_TURNS,
    DEFAULT_TOOL_RESULT_MAX_CHARS,
    MAX_AUTO_CONTINUE_ATTEMPTS,
    REASONING_ONLY_CONTINUE_ATTEMPTS,
    SOFT_CONTINUE_NUDGE_ENABLED,
    LLM_MAX_RETRIES,
    LLM_RETRY_BASE_DELAY,
    LLM_RETRY_MAX_BACKOFF,
    TOKEN_ESTIMATE_CHAR_DIVISOR,
    STREAM_MAX_SILENCE_SECONDS,
    STREAM_MAX_TOTAL_SECONDS,
)
from agent_cascade.retry_policy import classify_error, calculate_backoff, RetryPolicy

from agent_cascade.llm.schema import (
    ASSISTANT, FUNCTION, SYSTEM, USER, Message,
)
from agent_cascade.log import logger
from agent_cascade.exceptions import (
    CharacterRunDetected,
    MaxTokenExceeded,
    ContextWindowExceeded,
    FallbackCompressionRequired,
    AgentTerminatedError,
)
from agent_cascade.tool_utils import (
    MAX_SPILL_SIZE,
    mark_tool_call_truncated,
    was_tool_call_truncated,
    clear_truncation_state,
    generate_spillover_filename,
    resolve_cached_entry_refs,
    apply_cached_entry_resolutions,
)
from agent_cascade.utils.utils import extract_text_from_message, get_message_stats, msg_field, msg_set

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.lifecycle_manager import AgentLifecycleManager
from agent_cascade.compression.handler import CompressionHandler
from agent_cascade.tool_dispatcher import ToolDispatcher
from agent_cascade.stream_publisher import StreamPublisher
from agent_cascade.inner_loop_detect import InnerLoopDetector, save_loop_sample
from agent_cascade.settings import InnerLoopSettings as _InnerLoopSettings
from agent_cascade.operation_manager import set_current_instance_name, clear_current_instance_name

# ── Constants (core) ───────────────────────────────────────────────────────────
SLEEPING_LOOP_BACKOFF = 0.1              # Seconds to sleep when re-entering loop from SLEEPING state
_COMPRESSION_WAIT_TIMEOUT = 1.0          # Seconds to wait per iteration when suspended by compression
REACQUIRE_TIMEOUT = 30.0                 # Bounded FAST re-acquire window (post-yield fast path); on timeout the instance re-enters FIFO at tail (unbounded)

# MAX_TEXT_LENGTH_FOR_REGEX / MIN_OUTPUT_LENGTH now live in helpers.py (their true
# home — used by the helper functions there); re-imported below alongside helpers.
# SAMPLING_AND_LIMIT_KEYS lives in llm_call.py (used by _build_merged_cfg).

from agent_cascade.engine.helpers import (
    MAX_TEXT_LENGTH_FOR_REGEX,
    MIN_OUTPUT_LENGTH,
    SleepAction,
    _build_resources_block,
    _build_session_metadata,
    _replace_section,
    _replace_resources_block,
    _inject_skills_to_system_message,
    _check_message_truncation,
    _is_incomplete_state,
    _extract_tool_calls_from_text,
    _normalize_gemma_thought_tags,
    _normalize_thinking_blocks,
)
from agent_cascade.engine.llm_call import LLMCallMixin
from agent_cascade.engine.compression_exec import CompressionExecMixin
from agent_cascade.engine.tool_execution import ToolExecMixin


class ExecutionEngine(LLMCallMixin, CompressionExecMixin, ToolExecMixin):
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

        # Second phase of two-phase init: wire engine refs now that all handlers exist.
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
        # stream_publisher doesn't need an engine reference

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
            # Store slot key for diagnostics. Cursor-aware resolution (sticky slot
            # plan change #6): the key must match the endpoint pool that
            # _acquire_slot actually acquired (chain rotated by this instance's
            # cursor), not the raw chain head.
            if instance._slot_release is not None:
                router = self.pool.api_router
                if router:
                    if hasattr(router, 'get_effective_slot_info'):
                        slot_info = router.get_effective_slot_info(
                            instance.agent_class, instance_name=instance.instance_name
                        )
                    else:
                        slot_info = router.get_agent_slot_info(instance.agent_class)
                    instance._slot_key = slot_info.get('slot_key')
            logger.debug(
                f"[SLOT_ACQUIRE] {context} - instance={instance.instance_name}, "
                f"class={instance.agent_class}"
            )
        except AgentTerminatedError:
            # Clean abort — don't log as error, just propagate for caller to handle
            raise
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
        log_inst = self.pool.get_logger(inst_name, agent_class)
        if lock_held:
            instance.append_message(msg)
            log_inst.log_message(msg)
        else:
            with instance._compression_lock:
                instance.append_message(msg)
                log_inst.log_message(msg)

    def _append_and_log_batch(
        self,
        instance: AgentInstance,
        msgs: List[Message],
        *,
        lock_held: bool = False  # Caller already holds _compression_lock (RLock)
    ) -> None:
        """Append multiple messages to conversation AND log them atomically under compression lock."""
        if not msgs:
            return
        inst_name = instance.instance_name
        agent_class = instance.agent_class
        log_inst = self.pool.get_logger(inst_name, agent_class)
        if lock_held:
            instance.append_messages(msgs)
            for msg in msgs:
                log_inst.log_message(msg)
        else:
            with instance._compression_lock:
                instance.append_messages(msgs)
                for msg in msgs:
                    log_inst.log_message(msg)

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

        # Drain pending compression notifications into the first USER message
        # (in-tool-response pattern)
        if self.compression_handler and processed_messages:
            first_msg = processed_messages[0]
            try:
                if isinstance(first_msg.content, str):
                    first_msg.content = self.compression_handler._drain_pending_into_user_message(instance, first_msg.content)
                    # Also drain generic tool warnings into USER messages
                    # (appended)
                    first_msg.content = self.compression_handler._drain_tool_warnings(instance, first_msg.content, prepend=False)
                    # Also drain cache notifications into USER messages
                    # (prepended)
                    first_msg.content = self.compression_handler._drain_cache_notifications(instance, first_msg.content, prepend=True)
            except Exception as e:
                logger.debug(f"Drain failed for {inst_name} (non-critical): {e}")

        with instance._compression_lock:
            for msg in processed_messages:
                # Append to response accumulator (separate list for local use)
                response.append(msg)
                self._append_and_log(instance, msg, lock_held=True)

        # Mark activity OUTSIDE the lock to reduce hold time (Fix
        # Call once per message, not in a nested loop
        try:
            for _ in processed_messages:
                self.pool._mark_activity(inst_name)

            # ── Tail sync check after drain+inject logging (design doc §5.2 —
            # D1 fix) ──
            if getattr(self.pool.settings, 'tail_sync_check_enabled', True):
                from agent_cascade.logger.tail_sync_check import check_and_log as _check_tail
                with instance._compression_lock:
                    conv = instance.conversation
                log_inst = self.pool.get_logger(inst_name, instance.agent_class)
                _check_tail(inst_name, conv, log_inst.log_path, context="drain_inject")
        except Exception as e:
            logger.debug(f"Logging failed for {inst_name} (non-critical): {e}")

        # Proactive compression check after async child results are injected
        if processed_messages:
            self._proactive_compression_check(instance, messages, llm_messages, response, check_label="async-drain")

        return True

    def _clear_llm_preprocess_cache(self, instance: 'AgentInstance', inst_name: str) -> None:
        """Clear the LLM preprocessing cache for an instance's template.

        Extracted to eliminate duplicate cache-clearing blocks (M11 fix).
        Silently swallows errors — cache clearing is best-effort.
        """
        template = self.pool.get_template(instance.agent_class)
        if template and hasattr(template, 'llm') and template.llm:
            try:
                template.llm._clear_preprocess_cache()
            except Exception as e:
                logger.debug(f"Failed to clear LLM preprocess cache for {inst_name}: {e}")


    @staticmethod
    def _make_user_message(text: str) -> Message:
        """Create a USER message from raw text."""
        return Message(role=USER, content=text)

    @staticmethod
    def _make_async_result_message(tuple_data: Tuple[str, Optional[str]]) -> Message:
        """Create a USER message from an async result tuple (content, function_id)."""
        result_content, function_id = tuple_data
        # Don't wrap shell_cmd messages—they already have their own structured prefix
        stripped = result_content.strip()
        if stripped.startswith('⟨shell_cmd'):
            return Message(role=USER, content=result_content)
        # Don't double-wrap agent results that already have [Agent ...] prefix
        if stripped.startswith('[Agent '):
            return Message(role=USER, content=result_content)
        prefix = f"[BACKGROUND TOOL RESULT for {function_id}]" if function_id else "[BACKGROUND TOOL RESULT]"
        return Message(role=USER, content=f"{prefix}: {result_content}")

    # ═══════════════════════════════════════════════════════════════════════
    #  Main Execution Loop — Core turn loop orchestration and phase dispatch
    # ═══════════════════════════════════════════════════════════════════════


    def run(self, instance: AgentInstance) -> Iterator[Union[List[Message], tuple[List[Message], bool], None]]:
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
                # Safety net: If we reach here, the L1 session_lock guard in
                # api_server.py
                # failed to prevent a race condition. Raise to surface the bug
                # instead of
                # silent return.
                raise RuntimeError(
                    f"[BUG] {instance.instance_name} entered engine.run() in state "
                    f"{instance.state.name} — should be IDLE. L1 race guard failed!"
                )
        self._current_instance = instance  # Fix #2: set for token count cache lookups

        # Capture run generation to detect if a newer execution has superseded
        # this one.
        # When user clicks Stop then Resume, pool._run_generation is
        # incremented and
        # the old thread exits here instead of continuing with stale state.
        # NOTE: The shared ExecutionEngine's _my_generation can be overwritten
        # by sub-agents
        # via _create_and_run_agent(). However, pool.stopped provides
        # defense-in-depth,
        # so even if _my_generation is clobbered, the stop signal will still be
        # detected.
        self._my_generation = self.pool._run_generation

        # Clear truncation state at the start of each agent turn to prevent
        # stale markers
        clear_truncation_state()
        instance._loop_rollback_count = 0

        # Initialize variables before try block to handle exceptions during
        # _setup_turn
        messages = None
        llm_messages = None
        response = None

        # ── Acquire concurrency slot for this agent's endpoint ───────────────
        # On sequential endpoints (concurrency_limit=0), only one agent should
        # be making API calls at a time. The parent acquires the slot, then
        # releases
        # it when transitioning to SLEEPING so children can proceed.

        # Acquire concurrency slot (single FIFO queue per endpoint — no bypass).
        # Nested agents (Security, Compressor) now yield their caller's slot before
        # running, so they acquire normally here instead of inheriting the parent's slot.
        #
        # NOTE: The terminal-stop guards below live INSIDE the try block on purpose.
        # They run after the RUNNING transition above but must pass through this
        # generator's exit finally (RUNNING→IDLE transition). Returning before
        # `try:` skips that finally entirely and leaves the instance stuck in
        # RUNNING state — the next engine.run() entry then trips the L1 race guard.

        # Sticky slot plan change #7 (defensive, no behavior change): a non-None
        # _slot_release here means a previous run leaked its permit without going
        # through a release point — the stale-clear below would orphan it and pin
        # the pool forever. Log loudly so the leak is findable; keep the clear as-is.
        if getattr(instance, '_slot_release', None) is not None:
            logger.warning(
                f"[SLOT_LEAK_GUARD] run() entry for '{instance.instance_name}' found a "
                f"non-None _slot_release (stale permit from key={getattr(instance, '_slot_key', None)}). "
                f"Clearing without releasing — a release point was skipped upstream."
            )
        instance._slot_release = None  # Initialize for proper cleanup in finally block
        instance._slot_key = None      # Clear stale slot key from previous run (if any)
        instance._compression_suspended_at = 0.0  # Reset per-run suspension marker (BUG-4/8 exit-finally)

        try:
            if self._is_terminal_stop(instance.instance_name):
                return  # Terminal stop — don't start work (exit finally → IDLE)

            self._acquire_slot_with_logging(instance, "initial")

            # Exit if stopped after slot acquire — prevents stale slot reuse post-stop
            if self._is_terminal_stop(instance.instance_name):
                self._release_slot(instance, instance.instance_name, action="drop-exit")
                return  # Terminal stop — release slot and exit (exit finally → IDLE)

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
                # Safety: drain any queued user messages before exiting, so
                # they aren't lost.
                # Note: Using pool.add_message() here is safe because the
                # engine returns immediately
                # after this block (line 676), so cached lists are never used
                # again for this instance.
                # This prevents reintroducing the silent cache rebuild bug from
                # Fix 1.
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

                # ── Tail sync check after early-exit logging (design doc §5.2
                # — D1 fix) ──
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

                # Flag to track if we disabled tools this iteration (for safe
                # cleanup)
                final_turn_tools_disabled = False

                # ── SLEEPING STATE GUARD ────────────────────────────────────
                # Agents wake on ANY queued message (user messages or async tool results).
                # Both types now use the same message_queue; unified wakeup simplifies flow.
                if instance.state == AgentState.SLEEPING:
                    action, yield_value = self._handle_sleeping_state(
                        instance, messages, llm_messages, response
                    )
                    if yield_value is not None:
                        yield yield_value
                        if action == SleepAction.CONTINUE_LOOP:
                            time.sleep(SLEEPING_LOOP_BACKOFF)  # Prevent tight loop when no results available yet
                            continue
                    if action == SleepAction.BREAK_LOOP:
                        break
                    # Otherwise CONTINUE_LOOP — continue to next iteration

                # ── Phase 2: Pre-LLM Checks ────────────────────────────────
                # Stop/halt checks, async message injection, compression
                # check/force, loop detection
                # logger.debug(f"[PRE_LLM_CHECK] Checking
                # stop/halt/async/compression for {inst_name}")
                # Wrap turns_available in a mutable container so _pre_llm_checks can decrement it
                # when a "real cycle" occurs (rollback, compression, async injection).
                _turns = [turns_available]
                if self._pre_llm_checks(instance, messages, llm_messages, response, _turns):
                    logger.debug(f"[PRE_LLM_CHECK] Condition met, continuing loop")
                    yield response
                    if self._check_stop_conditions(instance):
                        break
                    turns_available = _turns[0]  # Sync back after possible decrement
                    continue

                # Turn limit warnings (50%, 90%, final) — one-time only, emitted as SEPARATE USER messages.
                # Checked BEFORE decrement so max_turns=1 agents still get the final warning.
                # Each threshold fires at most once (exact integer equality + single-step decrement).
                # Pattern: _make_user_message → _append_and_log → llm_messages.append.
                # NOTE: these are NOT added to the local `messages` list because _setup_turn()
                # runs ONCE per run (not per iteration); loop detection/compression thus see a view
                # that omits them — same as the final-turn warning. Bounded (<=2 messages) and does
                # not affect LLM context, which uses llm_messages.
                if turns_available == turns_50pct:
                    warn_msg = (
                        f"[SYSTEM WARNING: Halfway through your turn budget. "
                        f"You have {turns_available} turn(s) remaining out of {max_turns} total. "
                        f"Assess your progress and plan remaining steps.]"
                    )
                    warn_user = self._make_user_message(warn_msg)
                    self._append_and_log(instance, warn_user)
                    llm_messages.append(warn_user)
                if turns_available == turns_90pct:
                    warn_msg = (
                        f"[SYSTEM WARNING: Turn limit approaching. "
                        f"You have {turns_available} turn(s) remaining out of {max_turns} total. "
                        f"Plan your remaining steps carefully.]"
                    )
                    warn_user = self._make_user_message(warn_msg)
                    self._append_and_log(instance, warn_user)
                    llm_messages.append(warn_user)
                if turns_available == 1:
                    # Final turn warning: insert as a separate user message
                    # (not inline)
                    # so it's treated as a distinct conversational turn, not
                    # appended to the last message
                    final_msg = self._make_user_message(
                        f"[SYSTEM WARNING: Final turn. You have 1 turn left to complete your task. "
                        f"Wrap up and deliver your results now.]"
                    )
                    self._append_and_log(instance, final_msg)
                    llm_messages.append(final_msg)

                    # Disable ALL tools on the last turn so agent is forced to
                    # return a final answer
                    template = self.pool.get_template(instance.agent_class)
                    if template and hasattr(template, 'function_map'):
                        all_tools = list(template.function_map.keys())
                        if all_tools:
                            if not hasattr(instance, '_generate_cfg_override') or instance._generate_cfg_override is None:
                                instance._generate_cfg_override = {}
                            instance._generate_cfg_override['disabled_tools'] = all_tools
                            final_turn_tools_disabled = True

                turns_available -= 1

                # ── Phase 3: LLM Call with Injection Points ────────────────
                # logger.debug(f"[LLM_CALL_START] Calling LLM for {inst_name}
                # with {len(llm_messages)} messages")
                turn_output = []
                partial_msgs = []  # Initialize to avoid undefined reference on early break
                stream_tick = 0  # Counter for periodic termination checks during streaming
                terminated_during_stream = False  # Track if we already yielded a termination result inside the loop

                gen = self._call_llm_with_injection(instance, llm_messages)
                try:
                    for msg in gen:
                        if msg is None:
                            # Yield current partial conversation state to
                            # trigger streaming broadcast in
                            # run_agent_thread_unified.
                            # We combine persisted history (response),
                            # committed turn messages (turn_output),
                            # and currently streaming partial messages
                            # (instance._streaming_responses)
                            # to provide a complete "current view" for activity
                            # banners and UI rendering.
                            with instance._compression_lock:
                                partial_msgs = list(instance._streaming_responses)

                            stream_tick += 1
                            if (result := self._check_stream_termination(
                                stream_tick, inst_name, response, turn_output, partial_msgs
                            )) is not None:
                                yield result
                                terminated_during_stream = True
                                break

                            yield (response + turn_output + partial_msgs, True)
                            continue
                        # FIX BOOL_LEAK: Validate message type before appending
                        # to prevent bool/list leak
                        if isinstance(msg, (Message, dict)):
                            # Endpoint recovery: [RETRYING] messages are
                            # transient UI notifications only — don't add to
                            # conversation history
                            content = msg_field(msg, 'content', '')
                            is_retrying_msg = isinstance(content, str) and content.startswith("[RETRYING]")

                            stream_tick += 1
                            if (result := self._check_stream_termination(
                                stream_tick, inst_name, response, turn_output, partial_msgs
                            )) is not None:
                                yield result
                                terminated_during_stream = True
                                break

                            # Yield for UI visibility (even transient messages)
                            # Retry notifications show only the retry message;
                            # normal messages show with streaming state
                            if is_retrying_msg:
                                yield (response + turn_output + [msg], True)
                            else:
                                yield (response + turn_output + partial_msgs, True)

                            # Only append to turn_output if it's a real message
                            # (not transient retry notification)
                            if not is_retrying_msg:
                                turn_output.append(msg)
                        else:
                            logger.warning(f"[MSG_VALIDATION] Skipping non-Message in LLM response for {instance.instance_name}: type={type(msg).__name__}, value={str(msg)[:100]}")
                finally:
                    gen.close()  # Ensure generator cleanup on early break (prevents resource leak)

                # Check generation change (old run superseded by newer one)
                # alongside stop
                if not terminated_during_stream:
                    if self._is_terminal_stop(instance.instance_name):
                        logger.debug("terminal stop - %s", instance.instance_name)
                        yield response
                        break
                    elif self._is_suspended_by_compression(instance.instance_name):
                        # Compression-halt is a suspension, not termination — wait cooperatively.
                        logger.debug("suspended by compression, waiting - %s", instance.instance_name)
                        if not self._wait_for_compression_to_clear(instance.instance_name):
                            yield response
                            break  # Terminal stop during wait
                        yield response
                        continue  # Resumed (compression done), continue the main loop with this turn's output

                # Clean up last-turn tool disabling only if we set it this
                # iteration
                if final_turn_tools_disabled:
                    if hasattr(instance, '_generate_cfg_override') and isinstance(instance._generate_cfg_override, dict):
                        instance._generate_cfg_override.pop('disabled_tools', None)

                # logger.debug(f"[LLM_DONE] {inst_name} got {len(turn_output)}
                # messages from LLM")
                # ── Phase 4: Response Processing and Tool Execution ─────────
                if self._process_response(instance, turn_output, messages, llm_messages, response):
                    # NOTE: auto-continue deliberately does NOT reset turns_available — the
                    # per-iteration decrement (turns_available -= 1) already ran before this
                    # LLM call, so every auto-continue consumes exactly one real turn and
                    # max_turns stays a hard budget.
                    # logger.debug("tool used - %s looping",
                    # instance.instance_name)
                    yield response
                    continue

                # ── Phase 5: Post-Turn Checks ───────────────────────────────
                if not self._post_turn_checks(instance, messages, llm_messages, response):
                    break

            # ── Cleanup: Turn limit reached ────────────────────────────────
            if turns_available <= 0:
                # Inject turn limit notice into the LAST assistant message
                # content
                # instead of appending a new message. This ensures
                # extract_instance_output()
                # (which reads messages[-1]) returns the agent's actual output
                # with the warning.
                turn_notice = "\n\n[Turn limit reached — results may be incomplete. Continue if needed.]"
                notice_appended = False
                if instance.conversation:
                    # Find the last assistant message with text content and
                    # append the notice
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
                    # Fallback: if no assistant message with text found, append
                    # a new one
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

        except AgentTerminatedError:
            # Clean abort from termination during tool execution or slot acquire.
            # Don't log as error, don't yield error message — just exit cleanly.
            logger.debug(f"[DISMISSAL] Aborting run() for {instance.instance_name}: terminated")
        except Exception as e:
            # C4 fix: Catch unhandled exceptions — log and yield error state
            logger.error("EXCEPTION - %s: %s: %s", instance.instance_name, type(e).__name__, e, exc_info=True)
            # Telemetry: record turn end on exception (non-blocking)
            if (tel := self._telemetry()) is not None:
                try:
                    tel.record_turn_end(instance.instance_name)
                except Exception:
                    pass
            error_msg = Message(role=ASSISTANT, content=f"[SYSTEM ERROR: {e}]")
            yield [error_msg]

        finally:
            # C4 fix: Always clean up — transition to IDLE regardless of how we
            # exit

            inst_name = instance.instance_name
            suspended_this_run = getattr(instance, '_compression_suspended_at', 0.0) > 0.0
            terminated = self._is_terminal_stop(inst_name)
            outstanding = self.pool.has_pending(inst_name) or self.pool.has_messages(inst_name)

            # BUG-8 FIX: preserve wakeups ONLY when a suspension-driven exit left real
            # work behind. Normal completions and terminal stops behave exactly as before.
            preserve = suspended_this_run and not terminated and outstanding

            if not preserve:
                if hasattr(self.pool, '_async_registry'):
                    try:
                        self.pool._async_registry.clear_pending(instance.instance_name)
                    except Exception:
                        pass  # Non-critical cleanup
                # Also drain the message queue to prevent memory leak from stale messages
                if hasattr(self.pool, 'drain_queue'):
                    try:
                        self.pool.drain_queue(instance.instance_name)
                    except Exception:
                        pass  # Non-critical cleanup

            # FIX Critical
            with instance._compression_lock:
                if instance._continue_saved_msg is not None:
                    logger.debug(
                        f"[CONTINUE_FIX] Continue saved message not merged (merge path skipped) for {instance.instance_name}. "
                        f"Content is in conversation; this is expected on early exit."
                    )
                    instance._continue_saved_msg = None

            # Release concurrency slot on exit if still held (using helper
            # method FIX Mi3). Structured drop-exit event (change #10c).
            self._release_slot(instance, instance.instance_name, action="drop-exit")

            # FIX LogAppendFixer: Final sync to ensure all messages in
            # conversation are logged
            # This catches any injected messages that weren't followed by an
            # LLM call triggering _process_response() sync
            try:
                inst_name = instance.instance_name
                agent_class = instance.agent_class
                log_inst = self.pool.get_logger(inst_name, agent_class)

                already_logged_count = len(log_inst.data.get("history", []))
                with instance._compression_lock:
                    conv_len = len(instance.conversation)

                # Only sync if there's a mismatch (defensive check to avoid
                # redundant logging)
                if already_logged_count < conv_len:
                    logger.debug(
                        f"[FINAL_SYNC] {inst_name}: Catching up {conv_len - already_logged_count} unlogged messages "
                        f"(logged={already_logged_count}, conversation={conv_len})"
                    )
                    with instance._compression_lock:
                        conv = list(instance.conversation)
                        # Catch-all: messages are already in conversation; only
                        # JSONL needs catching up.
                        for msg in conv[already_logged_count:]:
                            if isinstance(msg, Message) or (isinstance(msg, dict) and 'role' in msg):
                                log_inst.log_message(msg)

                        # ── Tail sync check after final sync (design doc §5.2
                        # — D1 fix) ──
                        if getattr(self.pool.settings, 'tail_sync_check_enabled', True):
                            from agent_cascade.logger.tail_sync_check import check_and_log as _check_tail
                            _check_tail(inst_name, conv, log_inst.log_path, context="final_sync")
            except Exception as e:
                logger.debug(f"Final sync to JSONL failed for {getattr(instance, 'instance_name', 'unknown')} (non-critical): {e}")

            with instance._state_lock:
                current_state = instance.state
                if current_state in (AgentState.RUNNING, AgentState.SLEEPING, AgentState.COMPLETING):
                    # Mark activity at task completion so idle timer starts
                    # from when agent becomes idle, not from creation
                    self.pool._mark_activity(instance.instance_name)
                    # BUG-4 FIX: exiting with preserved outstanding work → SLEEPING
                    # (idle-checker protected; agents wake from queued messages in
                    # either state). Everything else → IDLE as before.
                    target = AgentState.SLEEPING if preserve else AgentState.IDLE
                    instance._transition(target)
                    if preserve:
                        instance.sleeping_since = time.monotonic()
                    logger.debug(
                        "EXIT - %s %s→%s%s", instance.instance_name, current_state.name,
                        target.name, " [suspension-preserved]" if preserve else ""
                    )
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

        # Restore KV state ONLY if this instance currently holds a concurrency slot,
        # and target the endpoint it actually holds — never the stale
        # _last_endpoint_config (which may point at a shared conc=0 autoloader we no
        # longer own; loading there would auto-evict a live sibling's resident model).
        # The FIFO guarantees single-holder for conc=0, so restoring while holding the
        # slot can never evict another agent. A missed restore is far better than a
        # wrongful eviction — any resolution failure skips the restore.
        if instance._slot_release is None:
            logger.debug("[STATE_RESTORE_SKIP] %s holds no slot — skipping state restore", inst_name)
        else:
            self._restore_held_slot_state(instance, inst_name)

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
            # Extend cached lists with any new messages appended since last
            # turn
            with instance._compression_lock:
                cached_len = len(instance._cached_messages)
                current_len = len(instance.conversation)

                # Fix 2: Cache sanity check - detect mismatches that would
                # cause silent rebuilds
                if cached_len != current_len:
                    if current_len > cached_len:
                        # Normal case: new messages were appended - extend the
                        # cache
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
                        # cached_len > current_len indicates a regression in
                        # Fix 1 — atomic updates should prevent this.
                        # Force rebuild to resync, log at INFO level for
                        # visibility (Fix 2 + Fix 3).
                        logger.info(
                            f"[CACHE_MISMATCH] {inst_name}: conv={current_len}, cached={cached_len} "
                            f"— forcing rebuild to resync"
                        )
                        can_use_cache = False

                if can_use_cache:
                    logger.debug(f"[CACHE_HIT] Reusing cached messages={len(instance._cached_messages)}, llm_messages={len(instance._cached_llm_messages)}")
                    return instance._cached_messages, instance._cached_llm_messages, []

        # Cache miss or config change - rebuild from pool (Fix 3: promoted to
        # INFO for visibility)
        logger.info(f"[CACHE_REBUILD] Rebuilding working set for {inst_name} (conv_len={len(conv)})")

        # Load template to get system message if needed
        template = self.pool.get_template(instance.agent_class)

        # P7: System prompt injection for ALL agents (not just root)
        # Inject identity, session metadata, available resources, and argument
        # reuse instructions
        if len(conv) > 0:
            m0 = conv[0]
            m0_role = m0.get('role') if isinstance(m0, dict) else getattr(m0, 'role', '')

            # If no system message at start, inject it from template
            if m0_role != SYSTEM and template and getattr(template, 'system_message', None):
                sys_msg = Message(role=SYSTEM, content=template.system_message)

                # Preserve old first message's timestamp so update_history()
                # can match it as an update
                # rather than appending a duplicate. The logger uses timestamps
                # as identity markers.
                if len(conv) > 0 and hasattr(conv[0], 'timestamp') and conv[0].timestamp:
                    try:
                        sys_msg.timestamp = conv[0].timestamp
                    except AttributeError:
                        pass

                instance.insert_message_at_head(sys_msg)  # PR2: centralized API handles cache sync and clear
                m0 = sys_msg
                m0_role = SYSTEM

                # Note: No update_history() call here -
                # _log_messages_to_jsonl() already handles first-time logging.
                # Calling update_history() with fresh timestamp causes dedup to
                # fail and create duplicates.

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
                            # First injection — insert after the identity line
                            # (same position as before)
                            content_lines = m0_content.split('\n')
                            insert_pos = 2 if len(content_lines) > 1 and not content_lines[1].startswith("#") else 1
                            for i, ml in enumerate(meta_block.split('\n')):
                                content_lines.insert(insert_pos + i, ml)
                            m0_content = '\n'.join(content_lines)

                    # 3. Inject/update available resources (agent types and
                    # cache pool note based on feature flags)
                    # Note: Using already-resolved template, no need for
                    # re-lookup
                    new_block = _build_resources_block(self.pool, template, instance)
                    if new_block:
                        if '## AVAILABLE AGENTS' in m0_content:
                            # Replace the existing block with fresh data
                            # (handles dynamic tool changes)
                            m0_content = _replace_resources_block(m0_content, new_block)
                        else:
                            # Legacy migration support only: strip the old "--- CURRENT AVAILABLE RESOURCES"
                            # block from restored sessions that predate the "## AVAILABLE AGENTS" format.
                            if '--- CURRENT AVAILABLE RESOURCES' in m0_content:
                                escaped_old = re.escape("--- CURRENT AVAILABLE RESOURCES")
                                old_pattern = escaped_old + r'.*?(?=\n\n(?:#{1,6}|---)|\Z)'
                                m0_content = re.sub(old_pattern, "", m0_content, count=1, flags=re.DOTALL)

                            # First injection — insert before ## Active Skills if present,
                            # otherwise append at end. This ensures consistent order:
                            # identity/metadata → available agents → active skills
                            if '## Active Skills' in m0_content:
                                # Insert before the Active Skills section
                                m0_content = m0_content.replace('## Active Skills', new_block + '\n\n## Active Skills', 1)
                            else:
                                # No skills section — append at end as fallback
                                m0_content += new_block

                    # Update the message ONLY if content actually changed
                    # (preserves LLM prefix caching)
                    if m0_content != original_content:
                        # DEBUG: Show exactly what changed (for KV cache troubleshooting)
                        diff_summary = []
                        if len(m0_content) != len(original_content):
                            diff_summary.append(f"len {len(original_content)}→{len(m0_content)}")
                        if m0_content[-5:] != original_content[-5:]:
                            diff_summary.append(f"tail_diff")
                        # Find first diff position
                        first_diff = None
                        for i, (a, b) in enumerate(zip(original_content, m0_content)):
                            if a != b:
                                first_diff = i
                                break
                        if first_diff is not None:
                            ctx_start = max(0, first_diff - 30)
                            ctx_end = min(first_diff + 30, len(original_content))
                            diff_summary.append(f"first_diff@{first_diff}: orig='{original_content[ctx_start:ctx_end]}' new='{m0_content[ctx_start:ctx_end]}'")

                        if isinstance(m0, dict):
                            m0['content'] = m0_content
                        else:
                            m0.content = m0_content
                        logger.debug(f"[CACHE_REBUILD] System prompt content CHANGED for {inst_name} ({', '.join(diff_summary)})")

                        # Persist updated system message to file so it survives
                        # restarts.
                        # After session load, rewrite_log_with_history()
                        # already wrote full history
                        # (63 msgs) and set _file_history_synced=True. Calling
                        # update_history()
                        # with only the working set (~8 msgs) does a surgical
                        # merge that can
                        # insert duplicates when messages are found at
                        # non-contiguous positions.
                        # Instead, directly update logger memory then rewrite
                        # via existing method.
                        try:
                            log_inst = self.pool.get_logger(inst_name, instance.agent_class)
                            if log_inst.data["history"]:
                                # Update the system message content in logger's
                                # in-memory history
                                formatted_sys = log_inst._format_message(m0)
                                log_inst.data["history"][0] = formatted_sys
                                # Rewrite file using existing method (handles
                                # handle flush, metadata, etc.)
                                log_inst.rewrite_log_with_history(log_inst.data["history"])
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
        # logger.info(f"[SETUP_TURN] messages={len(conv)},
        # llm_messages={len(llm_messages)}, roles={[m.get('role') if
        # isinstance(m, dict) else getattr(m, 'role', '?') for m in
        # llm_messages]}")
        return conv, llm_messages, response

    def _is_terminal_stop(self, inst_name: str) -> bool:
        """Check if this instance has a TERMINAL stop condition (cannot resume).

        Returns True only for conditions that mean execution must end permanently:
        - Global pool stop (user clicked Stop)
        - Generation mismatch (superseded by newer run)
        - Instance terminated

        Does NOT return True for compression-halt or manual halt — those are suspendable.
        """
        global_stop = self.pool.stopped
        gen_mismatch = self._my_generation != self.pool._run_generation
        inst_terminated = self.pool.is_instance_terminated(inst_name)
        result = global_stop or gen_mismatch or inst_terminated
        return result

    def _is_suspended_by_compression(self, inst_name: str) -> bool:
        """Check if this instance is suspended because of forced compression.

        Returns True only when the instance was halted by forced compression's
        halt_all_instances(). These agents should wait cooperatively and resume
        automatically when resume_all_instances() clears the flag.
        """
        return inst_name in self.pool._compression_halted

    def _wait_for_compression_to_clear(self, inst_name: str) -> bool:
        """Wait cooperatively while suspended by compression-halt.

        Compression-halt is a suspension, not termination — agents resume
        automatically once forced compression completes and clears the flag.

        BUG-1 FIX: yields the endpoint slot before blocking so other agents
        (including the Compressor required to clear this halt) can acquire it,
        then re-acquires via the standard bounded-FIFO path after resume.
        Without this, a slot-holding agent frozen mid-run starves the whole
        conc=0 pool for up to QUEUE_WAIT_TIMEOUT (circular-wait deadlock).

        Returns:
            True if compression cleared (agent should continue normally).
            False if a terminal stop occurred during wait (agent must exit).
        """
        instance = self.pool.get_instance(inst_name)

        # ── BUG-1 FIX: yield the slot before blocking on the halt flag ──────
        # Save KV state BEFORE release (matches sleep-transition ordering) so
        # our context survives while other agents share the conc=0 pool.
        if instance is not None:
            from agent_cascade.state_ops import save_instance_state
            with instance._state_lock:
                instance._compression_suspended_at = time.monotonic()  # BUG-4/8 marker
                save_instance_state(instance)
            self._release_slot(instance, inst_name, "compression_halt", action="drop-handoff")

        try:
            # ── BUG-6 FIX: plain sleep tick ─────────────────────────────────
            # The old `pool.wait_if_paused()` waits on the GLOBAL _paused event,
            # but compression-halt is per-instance (_halted_instances) — while
            # globally resumed every Event.wait(1.0) returned instantly, making
            # this a 100%-CPU hot loop for the entire suspension. A per-instance
            # halt has no event to wait on; sleep the same 1s tick instead.
            while self._is_suspended_by_compression(inst_name):
                if self._is_terminal_stop(inst_name):
                    return False  # Terminal stop — cannot resume; slot stays released (exit finally re-releases idempotently)
                time.sleep(_COMPRESSION_WAIT_TIMEOUT)
        finally:
            if not self._is_terminal_stop(inst_name):
                # Same re-acquire idiom as the tool_dispatcher sync-child path:
                # reacquire_for handles no-slot endpoints, bounded FIFO wait, and
                # (since the sticky-slot change) re-raises on hard scheduler failure.
                # Restore KV ONLY AFTER successful re-acquisition to avoid evicting
                # another agent's model.
                #
                # SANCTIONED EXCEPTION to the "no ungated state" rule (documented):
                # this is the ONLY place in the codebase where a slotless state is
                # tolerated. It's a system path — if re-acquisition fails here (hard
                # scheduler failure raised, OR reacquire_for returning False for a
                # missing router), a stuck compression-resume is worse than a degraded
                # one, so we swallow it and let compression resume WITHOUT re-acquiring
                # its slot. Every other reacquire_for caller lets the exception propagate.
                try:
                    ok = self.reacquire_for(instance, inst_name, context="after_compression_resume")
                except Exception as e:
                    logger.error(
                        f"[SLOT_REACQUIRE_DEGRADED] compression-resume for '{inst_name}' "
                        f"failed to re-acquire its slot (pool={getattr(instance, '_slot_key', None) if instance else 'unknown'}); "
                        f"continuing WITHOUT a re-acquired slot (sanctioned degrade): {e}",
                        exc_info=True,
                    )
                    ok = False
                if ok and instance is not None:
                    # Gate on ACTUAL slot ownership (ok=True also covers the no-slot /
                    # unlimited case where _slot_release is None), and target the held
                    # endpoint — never the stale _last_endpoint_config (eviction safety,
                    # same rationale as the _setup_turn gate).
                    if instance._slot_release is not None:
                        self._restore_held_slot_state(instance, inst_name)
        return True  # Compression cleared — safe to continue

    def _resolve_held_endpoint(self, instance: AgentInstance) -> Optional[dict]:
        """Resolve the endpoint the instance currently holds a slot on.

        Returns {'api_base', 'model'} for the cursor-aware effective endpoint (the
        same resolution used at slot acquisition), or None if it cannot be resolved.
        Used by state-restore call sites so they only ever load onto an owned
        endpoint — loading a stale _last_endpoint_config on a shared conc=0 autoloader
        would auto-evict a live sibling's resident model.
        """
        router = self.pool.api_router if hasattr(self.pool, 'api_router') else None
        if not router or not hasattr(router, 'get_effective_slot_info'):
            return None
        slot_info = router.get_effective_slot_info(
            instance.agent_class, instance_name=instance.instance_name
        ) or {}
        api_base = slot_info.get('api_base') or ''
        # get_effective_slot_info carries no model — take it from the rotated chain head.
        chain = router.get_endpoint_chain(
            instance.agent_class, instance_name=instance.instance_name
        ) or []
        model = (chain[0].get('model') if chain else '') or ''
        if not api_base or not model:
            return None
        return {'api_base': api_base, 'model': model}

    def _restore_held_slot_state(self, instance: AgentInstance, inst_name: str) -> bool:
        """Restore KV state to the endpoint the instance currently holds a slot on.

        Defensive by design: any resolution failure skips the restore (a missed
        restore is far better than a wrongful eviction of a sibling's model).
        """
        try:
            held_cfg = self._resolve_held_endpoint(instance)
            if not held_cfg:
                logger.debug("[STATE_RESTORE_SKIP] %s — could not resolve held endpoint", inst_name)
                return False
            from agent_cascade.state_ops import restore_instance_state
            return restore_instance_state(instance, held_endpoint_cfg=held_cfg)
        except Exception as e:
            # Never evict on a resolution error — skip the restore.
            logger.debug("[STATE_RESTORE_SKIP] %s — endpoint resolution failed: %s", inst_name, e)
            return False

    def _check_stop_conditions(self, instance: AgentInstance) -> bool:
        """Check if we should skip the LLM call due to stop conditions.

        Only returns True for terminal stops — compression-halt agents should
        wait and retry rather than skipping the LLM call permanently.

        Extracted from _pre_llm_checks() - Phase 3.8

        Args:
            instance: Current agent instance

        Returns:
            True if any stop condition met (skip LLM call), False otherwise.
        """
        inst_name = instance.instance_name
        return self._is_terminal_stop(inst_name)

    def _is_stopped(self, inst_name: str) -> bool:
        """Check if execution should stop for this instance.

        Legacy name kept for backward compatibility at call sites that only need
        a boolean 'should I stop now'. Returns True for terminal stops OR any halt.

        IMPORTANT: Callers that break/return on this check must distinguish between
        terminal stops (break permanently) and compression-halt (wait then resume).
        Use _is_terminal_stop() and _is_suspended_by_compression() directly at those sites.

        Note: Does NOT include pause — pause is handled separately via cooperative
        wait loops (e.g. `while self.pool.is_paused(): time.sleep(0.1)`).
        Pause should not interrupt execution; it should just wait and resume.

        Args:
            inst_name: Instance name to check halt/termination status

        Returns:
            True if any stop condition met, False otherwise.
        """
        return (self._is_terminal_stop(inst_name) or
                inst_name in self.pool._halted_instances)

    def _check_stream_termination(
        self, stream_tick: int, inst_name: str, response: List[Message],
        turn_output: List[Message], partial_msgs: List[Message]
    ) -> Optional[Tuple[List[Message], bool]]:
        """Check for termination every N ticks during LLM streaming.

        Shared helper to avoid duplicating the 20-tick check pattern across multiple
        yield paths in the streaming loop. Returns (messages, is_streaming) tuple
        ready to yield, or signals a break via returning early.

        Args:
            stream_tick: Current tick counter (incremented each loop iteration)
            inst_name: Instance name for stop checks and logging
            response: Accumulated persistent response messages
            turn_output: Messages accumulated this turn (not yet committed to response)
            partial_msgs: Currently streaming partial responses

        Returns:
            Tuple of (messages_list, is_streaming_bool) ready to yield.
            Returns (response + turn_output + partial_msgs, False) if stop detected.
            Returns None if no stop detected (caller should continue normally).
        """
        if stream_tick % 20 == 0:
            stopped = self._is_terminal_stop(inst_name)
            if stopped:
                logger.debug(
                    "[TERMINATE] Stopped mid-stream after %d ticks - %s",
                    stream_tick, inst_name
                )
                return (response + turn_output + partial_msgs, False)
        return None

    def _inject_async_messages(
        self,
        instance: AgentInstance,
        messages: List[Message],
        llm_messages: List[Message],
        response: List[Message]
    ) -> bool:
        """Drain and inject user messages and async results that arrived during LLM call.

        Extracted from _pre_llm_checks() - Phase 3.8

        Also invalidates LLM preprocessing cache after queue injection for fresh processing.

        Args:
            instance: Current agent instance
            messages, llm_messages, response: Working message sets

        Returns:
            True if any messages were injected (need to re-process), False otherwise.
        """
        inst_name = instance.instance_name

        # Drain user messages from queue (includes async results — single queue now)
        if self._drain_and_inject(
            instance, inst_name, messages, llm_messages, response,
            drain_fn=self.pool.drain_queue,
            factory=self._make_user_message,
        ):
            # Invalidate LLM preprocessing cache after queue injection for fresh processing
            self._clear_llm_preprocess_cache(instance, inst_name)
            return True

        return False


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
                # all changes
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

        .. deprecated:: Use agent_cascade.retry_policy.classify_error() instead.
           Kept for backward compatibility; will be removed in a future phase.

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
            # Unknown error — default to retryable for transient issues we
            # haven't categorized
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


    def _handle_inner_loop_detection(
        self,
        instance: AgentInstance,
        e: Exception,
        retry_count: int,
        loop_retry_count: int,
        _max_attempts: int
    ) -> None:
        """Handle inner-loop detection (CharacterRunDetected/MaxTokenExceeded).

        Advances endpoint cursor when appropriate and checks loop budget exhaustion.
        Counters are NOT incremented here — they were already incremented by
        _abort_stream before the exception was raised.

        Dual counter semantics:
        - retry_count: shared budget counter for all retries (general + inner-loop)
        - loop_retry_count: tracks inner-loop-specific retries for observability
        Both draw from the same _max_attempts budget pool.

        Args:
            instance: Agent instance making the call
            e: The CharacterRunDetected or MaxTokenExceeded exception
            retry_count: Current shared retry counter (already incremented by _abort_stream)
            loop_retry_count: Current inner-loop-specific counter (already incremented by _abort_stream)
            _max_attempts: Maximum retry attempts from pool settings

        Raises:
            CharacterRunDetected: if retry budget exhausted (defense-in-depth check)
        """
        inst_name = instance.instance_name

        # Record inner-loop detection in telemetry (non-blocking — must never break the LLM call path).
        _det_reason = getattr(e, 'detection_reason', str(e)) or str(e)
        if (tel := self._telemetry()) is not None:
            try:
                tel.record_loop_detected(inst_name, reason=_det_reason, auto_rolled_back=False, pop_count=0, loop_type="inner")
            except Exception:
                pass

        # Check dedicated loop retry budget — fail fast if exhausted (_max_attempts from pool.settings.retry_max_attempts)
        # Note: generator already checks this before raising, so this is defense-in-depth.
        # Raising here matches original inline behavior for this edge case.
        if isinstance(e, CharacterRunDetected) and loop_retry_count >= _max_attempts:
            last_reason = getattr(e, 'detection_reason', str(e))
            raise CharacterRunDetected(
                f"inner_loop_exhausted: retried {_max_attempts} times, "
                f"giving up — last reason: {last_reason}",
                detection_reason=last_reason,
            )

        # Advance endpoint cursor only on character-run or max-token
        # detection so the next retry starts from a different endpoint
        # in the chain. Other detection types (sentence, ngram, block,
        # entropy, max-chars) should retry the same endpoint — they are
        # weaker signals. This is the "kick to next endpoint" mechanism
        # — without this, retries would try the same (failing) endpoint
        # again because call_with_fallback builds a fresh chain each time.
        _reason = getattr(e, 'detection_reason', '')
        if isinstance(e, (MaxTokenExceeded, ContextWindowExceeded)) or _reason.startswith('character run'):
            new_pos = self.pool.api_router.advance_instance_endpoint(inst_name)
            logger.warning(
                f"[INNER_LOOP] Endpoint cursor advanced for '{inst_name}' "
                f"to position {new_pos} (detection: {_reason}). "
                f"Next retry will use a different endpoint."
            )
        else:
            logger.info(
                f"[INNER_LOOP] Detection triggered for '{inst_name}' "
                f"(reason: {_reason}), but not strong enough to advance cursor. "
                f"Retrying same endpoint."
            )

    def _record_telemetry_event(self, inst_name: str, event_type: str, **kwargs) -> None:
        """Record telemetry event for LLM call lifecycle.

        Lightweight wrapper to reduce inline noise in retry logic.
        Failures are silently swallowed — telemetry must never break LLM calls.

        Args:
            inst_name: Agent instance name
            event_type: One of 'start', 'end', 'first_token'
            **kwargs: Event-specific data (input_tokens_est, output_tokens_est, model)
        """
        try:
            if tel := self._telemetry():
                if event_type == 'start':
                    tel.record_llm_call_start(inst_name, **kwargs)
                elif event_type == 'end':
                    last_output = kwargs.pop('last_output', None)
                    tel.record_llm_call_end(inst_name, output_tokens_est=kwargs.get('output_tokens_est', 0), last_output=last_output)
                elif event_type == 'first_token':
                    tel.record_llm_first_token(inst_name, **kwargs)
        except Exception:
            pass


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

            # Clean thinking blocks from function call arguments (P4
            # continuation) — REMOVED: json_loads already strips pre-parse,
            # and per-value normalization is a corruption risk.

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
        llm_messages: List[Message],
        response: List[Message]
    ) -> bool:
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
        if (is_truncated or is_incomplete) and not self._is_terminal_stop(inst_name) and self.pool.settings.auto_continue:
            instance._auto_continue_count = getattr(instance, '_auto_continue_count', 0) + 1
            if instance._auto_continue_count >= MAX_AUTO_CONTINUE_ATTEMPTS:
                # cap-hit reset (site a): clear ALL counters, give up entirely
                instance._auto_continue_count = 0
                instance._continue_fallback_append = False
                instance._reasoning_only_soft_attempts = 0
                instance._reasoning_only_pending_nudges = 0
                return False

            # NEW: reasoning-only gets a SOFT continue first (up to N times). Guarded by the
            # BUDGET counter, which never resets mid-episode, so once we fall through to full
            # retry this path is permanently closed for the episode (N3).
            if is_incomplete == "reasoning-only" and instance._reasoning_only_soft_attempts < REASONING_ONLY_CONTINUE_ATTEMPTS:
                instance._reasoning_only_soft_attempts += 1  # budget: how many soft tries this episode
                reason = f"incomplete state (reasoning-only, soft continue {instance._reasoning_only_soft_attempts}/{REASONING_ONLY_CONTINUE_ATTEMPTS})"
                if (tel := self._telemetry()) is not None:
                    try:
                        tel.record_auto_continue(inst_name, reason=reason)
                    except Exception:
                        pass
                logger.info(f"Detected incomplete state (reasoning-only) for {inst_name}. Soft continue attempt {instance._reasoning_only_soft_attempts}/{REASONING_ONLY_CONTINUE_ATTEMPTS}.")
                # Default: pure resend — re-call on the SAME history (reasoning msg stays in place); nothing is popped or appended.
                if SOFT_CONTINUE_NUDGE_ENABLED:
                    # (Deferred feature — OFF by default.) Inject an escalating USER nudge so the
                    # model gets an explicit instruction instead of a bare resend. When enabled, a
                    # nudge is now in context, so track it for rollback accounting on the later full retry.
                    instance._reasoning_only_pending_nudges += 1
                    self._inject_soft_continue_nudge(instance, inst_name, messages, llm_messages, response)
                return True

            # existing full-retry path (truncation / broken-json / empty-output / reasoning-only after N soft tries)
            reason = "truncation" if is_truncated else f"incomplete state ({is_incomplete})"
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
            if is_incomplete == "reasoning-only":
                pop_count += instance._reasoning_only_pending_nudges
            if pop_count > 0:
                self.pool._rollback_instance(inst_name, pop_count=pop_count)
                self._rebuild_working_set(messages, llm_messages, inst_name)
                response.clear()
            # N1 fix: the nudges were just popped above, so they no longer exist — zero ONLY the
            # pending-nudge counter. Do NOT touch _reasoning_only_soft_attempts (it must stay at N
            # so the soft path stays closed).
            instance._reasoning_only_pending_nudges = 0
            return True

        # normal completion -> reset both reasoning-only counters (site b)
        instance._auto_continue_count = 0
        instance._continue_fallback_append = False
        instance._reasoning_only_soft_attempts = 0
        instance._reasoning_only_pending_nudges = 0
        return False

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
            return ("You have produced reasoning-only output again with no visible result. "
                    "STOP thinking and MUST produce either a final answer or a concrete tool call "
                    "on this turn — do not emit another reasoning-only message.")
        return ("Your last turn contained only reasoning/thinking and no output or tool call. "
                "Please continue — produce your response text or make the next tool call now.")

    def _sync_conversation_log(self, instance: AgentInstance, inst_name: str, context: str) -> None:
        """Mark activity + run the tail-sync JSONL check after a conversation append (house pattern).

        Keeps the JSONL log in sync with ``instance.conversation`` on any path that appends to it.
        Best-effort: failures are logged at debug level and never propagate.
        """
        try:
            self.pool._mark_activity(inst_name)
            if getattr(self.pool.settings, 'tail_sync_check_enabled', True):
                from agent_cascade.logger.tail_sync_check import check_and_log as _check_tail
                with instance._compression_lock:
                    conv = instance.conversation
                log_inst = self.pool.get_logger(inst_name, instance.agent_class)
                _check_tail(inst_name, conv, log_inst.log_path, context=context)
        except Exception as e:
            logger.debug(f"Conversation log sync failed for {inst_name} (non-critical): {e}")

    def _inject_soft_continue_nudge(
        self, instance: AgentInstance, inst_name: str,
        messages: List[Message], llm_messages: List[Message], response: List[Message]
    ) -> None:
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
            messages.append(msg)          # full working set
            llm_messages.append(msg)      # LLM-formatted set
            response.append(msg)          # local accumulator (UI visibility)
            self._append_and_log(instance, msg, lock_held=True)  # conversation + JSONL log atomically
        # Keep the JSONL log in sync with the appended nudge (house pattern).
        self._sync_conversation_log(instance, inst_name, "reasoning_soft_continue")


    def _process_response(
        self, instance: AgentInstance, turn_output: List[Message],
        messages: List[Message], llm_messages: List[Message],
        response: List[Message]
    ) -> bool:
        """Phase 4: Normalize response, handle auto-continue on truncation, execute tools.

        Returns True if processing should continue to next iteration (tool was used or truncated).
        """
        inst_name = instance.instance_name

        # FIX
        is_truncated = any(_check_message_truncation(msg) for msg in turn_output)

        # Extracted to _normalize_turn_output() - Phase 3.3 (FIX
        self._normalize_turn_output(turn_output)

        self._append_and_log_batch(instance, turn_output)
        response.extend(turn_output)  # Separate list for streaming/accumulation
        # Streaming UI Content Update Fix: Clear _streaming_responses after
        # Phase 4 commits messages
        instance._streaming_responses = []

        # FIX: Option B - Merge continue-saved assistant message if present.
        # When Continue is clicked, the last assistant message was popped from
        # conversation
        # in api_server.py continue handler and stored as _continue_saved_msg.
        # We now merge
        # it with the newly generated assistant message to create a single
        # concatenated message.

        # FIX Minor
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

                        logger.debug(f"[CONTINUE_FIX] Merged continue-saved assistant message ({len(old_content)} chars) with new response ({len(new_content)} chars)")
                        merged = True
                        break

                if not merged:
                    # Fallback: saved message was popped from conversation by
                    # continue handler.
                    # If we can't merge it, re-append it to prevent data loss.
                    logger.warning(
                        f"[CONTINUE_FIX] Could not merge continue-saved message for {inst_name}: "
                        f"no assistant message found in turn_output. Re-appending as separate message."
                    )
                    self._append_and_log(instance, saved)
                    instance._continue_fallback_append = True

        # Extracted to _check_and_handle_truncation() - Phase 3.3
        if self._check_and_handle_truncation(is_truncated, turn_output, instance, inst_name, messages, llm_messages, response):
            return True  # Continue to next LLM call

        # Extracted to _execute_detected_tools() - Phase 3.3
        used_any_tool = self._execute_detected_tools(instance, inst_name, turn_output, messages, llm_messages, response)

        # Log ALL messages (turn_output + fn_msgs from tools) in a single delta
        # pass.
        # Called AFTER tool execution so FUNCTION results are already in conv
        # and get
        # picked up by the count-based delta sync — no risk of duplicate
        # logging.
        self._log_messages_to_jsonl(instance, inst_name, turn_output)

        # ── Post-tool urgent injection ───────────────────────────────────
        # Inject urgent messages AFTER all tools complete to avoid orphaned
        # tool_call_id's
        if self._drain_and_inject(
            instance, inst_name, messages, llm_messages, response,
            drain_fn=self.pool.drain_queue,
            factory=self._make_user_message,
        ):
            return True  # Continue to next LLM call for urgent message processing

        # FIX
        # the turn is complete (no continue message injected), so return False
        # to avoid infinite loop.
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
        # Check for pending async tool calls (including call_agent) before
        # completing
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

        # Safety drain: catch any messages from fast-completing children that
        # completed between register_async_call() and the has_pending() check above
        try:
            if self._drain_and_inject(
                instance, inst_name, messages, llm_messages, response,
                drain_fn=self.pool.drain_queue,
                factory=self._make_user_message,
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

        # Check stop immediately after LLM response — prevents unnecessary
        # post-turn processing
        if self._is_terminal_stop(inst_name):
            return False  # Terminal stop — break from main loop
        elif self._is_suspended_by_compression(inst_name):
            # Compression-halt is a suspension, not termination — wait, then re-process.
            logger.debug("post-turn processing suspended by compression - %s", inst_name)
            if not self._wait_for_compression_to_clear(inst_name):
                return False  # Terminal stop during wait
            # Resumed — fall through to process the response normally

        # 1. Check for unexecuted tool calls
        if self._check_for_tool_calls_in_output(instance, response):
            return True  # Tool was called — continue the loop (tool result will be in next turn)

        # 2. Transition to SLEEPING if async tools pending
        #    A pending background tool is real outstanding work and must take priority
        #    over the stall detector below — otherwise a parent that dispatched an async
        #    child then emitted a reasoning-only post-turn would wrongly break to IDLE
        #    before it could sleep, losing the child's later result.
        if self._transition_to_sleeping_if_pending(instance, inst_name):
            return True  # Continue loop → hits SLEEPING guard at top

        # 3. Detect pure thinking turn (stalled agent)
        #    Only reached when there is NO pending async work, so a genuinely stuck
        #    agent (no outstanding tools) still breaks out of the loop here.
        if self._detect_pure_thinking_turn(instance, response):
            return False  # Pure reasoning detected — break out of loop (agent stalled)

        # 4. Drain post-generation messages (safety drain)
        if self._drain_post_generation_messages(
            instance, inst_name, messages, llm_messages, response
        ):
            return True  # Messages drained — continue looping

        return False  # Agent has truly completed


    @staticmethod
    def _release_slot(slot_holder: Any, holder_name: str, context: str = "cleanup",
                      action: Optional[str] = None) -> None:
        """Release a concurrency slot from a slot holder with error handling.

        Thin delegate to :func:`agent_cascade.slot_queue.release_slot_permit` —
        the shared capture-nullify-release-log helper used by every sticky-slot
        lifecycle point. Kept as a staticmethod so callers (tool_dispatcher,
        slot_yield_utils, tests) can invoke it without an engine instance.

        Thread-safe: acquires instance._state_lock before checking/clearing _slot_release
        to prevent double-release with concurrent stop_session calls.

        Args:
            slot_holder: Object with _slot_release attribute (AgentInstance or similar)
            holder_name: Name of the holder for logging purposes
            context: Optional context description for logging (e.g., "sleep transition", "sync child")
            action: Optional structured-event label (sticky slot plan change #10c):
                'drop-sleep', 'drop-exit', 'drop-handoff'. When given, emits exactly one
                [SLOTPOOL] instance=... pool=... action=<action> waiters=<n> line.
        """
        from agent_cascade.slot_queue import release_slot_permit
        release_slot_permit(slot_holder, holder_name, action=action, context=context)


    def reacquire_for(self, instance: Any, holder_name: str, context: str = "reacquire") -> bool:
        """Re-acquire a concurrency slot for an agent after yielding it to a child.

        Public helper for the Security/Compressor yield/reacquire pattern (and any
        future parent-yields-for-child flow). Resolves the caller's endpoint via
        the router, then FIFO-acquires the slot with a bounded timeout.
        ``tool_dispatcher._reacquire_caller_slot()`` delegates to this method.

        The caller is expected to have already released its slot (via
        :meth:`_release_slot`) before running the child; this method puts it back.

        Args:
            instance: The AgentInstance that yielded its slot (has ``agent_class``,
                ``_slot_release``, ``_slot_key`` and ``_state_lock``).
            holder_name: Instance name, used as the SlotPool permit key + logging.
            context: Short label for log messages (e.g. "after_security_check").

        Returns:
            True if the slot was re-acquired (or no slot is needed). There is NO
            slotless degraded state (sticky slot plan change #5b / §3.9 Gap A): on a
            30s fast-path timeout the instance re-enters the FIFO at the tail and
            blocks until granted (unbounded, by design). False is returned only for
            hard failures (no router available); SlotCancelled and any other failure
            during the unbounded re-queue propagate to callers (never returns slotless).
        """
        # Lazy import to avoid a module-level circular dependency with api_router.
        from agent_cascade.slot_queue import SlotQueueTimeout, SlotCancelled

        if not instance:
            return False

        router = self.pool.api_router if hasattr(self.pool, 'api_router') else None
        if not router:
            logger.warning(f"[SLOT_REACQUIRE] No router available for '{holder_name}'")
            return False

        # Cursor-aware resolution (sticky slot plan change #5a): resolve the pool this
        # instance will ACTUALLY use next (chain rotated by its cursor), not the raw
        # chain head — otherwise a parent whose effective endpoint is a conc=0 fallback
        # would come back onto the primary's pool after yielding (G4).
        if hasattr(router, 'get_effective_slot_info'):
            slot_info = router.get_effective_slot_info(
                instance.agent_class, instance_name=holder_name
            )
        else:
            slot_info = router.get_agent_slot_info(instance.agent_class)
        if not slot_info or not slot_info.get('needs_slot'):
            # Unlimited endpoint — no slot to hold. Clear any stale state and done.
            with instance._state_lock:
                instance._slot_release = None
                instance._slot_key = None
            return True

        api_base = slot_info['api_base']
        concurrency_limit = slot_info['concurrency_limit']

        # Defensive fast-path: if the instance already holds a LIVE permit for exactly
        # the desired key, return without acquiring. SlotPool has no reentrant path —
        # an acquire() while holding the same capacity-1 pool self-deadlocks until the
        # timeout. This should never trigger under current usage (callers release first),
        # so a hit indicates unexpected usage and is logged as a warning.
        desired_key = slot_info.get('slot_key')
        with instance._state_lock:
            already_held = (
                getattr(instance, '_slot_key', None) == desired_key
                and getattr(instance, '_slot_release', None) is not None
            )
        if already_held:
            logger.warning(
                f"[SLOTPOOL] instance={holder_name} pool={desired_key} "
                f"action=sticky-keep (reacquire_for fast-path — unexpected: caller should "
                f"have released first; skipping acquire to avoid self-deadlock)"
            )
            return True

        try:
            release_cb = router.scheduler.acquire(
                api_base=api_base,
                concurrency_limit=concurrency_limit,
                instance_name=holder_name,
                agent_class=instance.agent_class,
                timeout=REACQUIRE_TIMEOUT,  # module constant — bounded FAST re-acquire window
            )
            if release_cb is not None:
                with instance._state_lock:
                    instance._slot_release = release_cb
                    # Track which slot key this agent holds (for diagnostics).
                    instance._slot_key = slot_info.get('slot_key')
                logger.debug(f"[SLOT_REACQUIRED] {context} - re-acquired slot for '{holder_name}'")
                return True
            else:
                # Unlimited — acquire returned None, no callback needed.
                with instance._state_lock:
                    instance._slot_release = None
                    instance._slot_key = None
                return True
        except SlotCancelled:
            # Terminated mid-wait — propagate the clean abort (caller handles).
            raise
        except (SlotQueueTimeout, TimeoutError):
            # Fast-window timeout. The pool raises SlotQueueTimeout, but we always
            # acquire through EndpointScheduler.acquire, which re-raises it as a plain
            # TimeoutError (scheduler.py) — so catch both. Falling through here means:
            # re-enter the FIFO at the tail (unbounded), below.
            pass

        # Sticky slot plan change #5b (user decision 3 / §3.9 Gap A): there is NO
        # slotless degraded state. The 30s fast window above only bounds the
        # post-yield fast path; on timeout the instance re-enters the FIFO at the
        # TAIL for its resolved effective slot and blocks until granted — unbounded,
        # by design (no timeouts, no bypass, no preemption). The old
        # [SLOT_REACQUIRE_FAILED] "degrade to async-only" path is deleted: it left a
        # conc=0 agent ungated, reintroducing the trashing window this project closes.
        logger.info(
            f"[SLOTPOOL] instance={holder_name} pool={slot_info.get('slot_key')} "
            f"action=acquire-queued waiters=-1 (post-yield fast re-acquire timed out after "
            f"{REACQUIRE_TIMEOUT:.0f}s — re-entering FIFO at tail, unbounded by design)"
        )
        try:
            release_cb = router.scheduler.acquire(
                api_base=api_base,
                concurrency_limit=concurrency_limit,
                instance_name=holder_name,
                agent_class=instance.agent_class,
                timeout=None,  # unbounded — blocks at FIFO tail by design
            )
        except SlotCancelled:
            raise
        except Exception as e:
            # A transient failure here must NOT strand the instance slotless (that would be
            # an ungated conc=0 state — plan §3.9). Log loudly and re-raise so the caller's
            # exception handling deals with it, matching how SlotCancelled propagates above.
            logger.error(
                f"[SLOT_REACQUIRE_FAILED] {context} for '{holder_name}' during "
                f"unbounded re-queue: {e}",
                exc_info=True,
            )
            raise
        if release_cb is not None:
            with instance._state_lock:
                instance._slot_release = release_cb
                instance._slot_key = slot_info.get('slot_key')
            logger.debug(
                f"[SLOT_REACQUIRED] {context} - re-acquired slot for '{holder_name}' "
                f"after unbounded FIFO wait"
            )
            return True
        # Unlimited — acquire returned None, no callback needed.
        with instance._state_lock:
            instance._slot_release = None
            instance._slot_key = None
        return True

    def _transition_to_sleeping(self, instance: 'AgentInstance') -> None:
        """Transition an agent instance to SLEEPING state.

        Helper method to reduce code duplication in _post_turn_checks.
        Sets the appropriate timestamps and transitions state atomically.
        Also releases the concurrency slot so children can proceed.

        Args:
            instance: The agent instance to transition.
        """
        # Note: This is safe because _release_slot checks for None before releasing.

        # Acquire state lock BEFORE releasing slot to prevent race
        # where another thread steals the slot between release and state transition.
        with instance._state_lock:
            if instance.state == AgentState.RUNNING:
                # Shared capture-nullify-release-log helper (slot_queue.release_slot_permit):
                # captures + nullifies under the state lock we already hold, then invokes
                # the callback outside it (the pool's condition may block on waiters).
                # The drop-sleep line is emitted only when a live permit was held.
                if instance._slot_release is not None:
                    # Save KV cache BEFORE releasing slot so context persists while
                    # other agents may use the same conc=0 pool during sleep.
                    from agent_cascade.state_ops import save_instance_state
                    save_instance_state(instance)

                    from agent_cascade.slot_queue import release_slot_permit
                    _sleep_pool = None
                    try:
                        _sched = getattr(self.pool, 'scheduler', None)
                        if _sched is not None:
                            _held_key = getattr(instance, '_slot_key', None)
                            _sleep_pool = _sched._pools.get(_held_key) if _held_key else None
                    except Exception:
                        pass
                    release_slot_permit(instance, instance.instance_name, action="drop-sleep",
                                        context="sleep transition", pool=_sleep_pool)

                # Mark activity before transitioning to SLEEPING so idle timer
                # is updated
                self.pool._mark_activity(instance.instance_name)
                instance._transition(AgentState.SLEEPING)
                instance.sleeping_since = time.monotonic()
                instance._last_wakeup_log = time.monotonic()
            else:
                # Log warning when transition is skipped to help identify bugs
                # where _transition_to_sleeping is called on agents not in RUNNING state.
                # This indicates a logic bug in the caller — the agent should be in RUNNING state before attempting to sleep it.
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
        response: List[Message]
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

        Returns:
            Tuple of (action, optional_yield_value):
            - action = CONTINUE_LOOP means re-enter the while loop
            - action = BREAK_LOOP means exit the while loop
            - yield_value=None means no special yield needed before continuing/breaking
            - yield_value=[] means yield empty list (signals waiting state)
        """
        inst_name = instance.instance_name

        # Check stop immediately — a SLEEPING agent should not wait up to 300s
        # for wakeup
        if self._is_terminal_stop(inst_name):
            return SleepAction.BREAK_LOOP, None
        # Compression-halt while sleeping: the sleep loop already polls; just continue waiting

        # Drain message queue — all wakeups now come through here (async results + user messages)
        messages_list = self.pool.drain_queue(inst_name)

        if messages_list:
            # Wake up on ANY message
            with instance._state_lock:
                if instance.state == AgentState.TERMINATED:
                    return SleepAction.BREAK_LOOP, None
                instance._transition(AgentState.RUNNING)
                instance.sleeping_since = None
                instance._last_wakeup_log = time.monotonic()
                logger.debug("RESUMED from SLEEPING - %s (%d messages)", inst_name, len(messages_list))

            # Inject all drained messages as user-type messages (async results are already formatted strings)
            self._drain_and_inject(
                instance, inst_name, messages, llm_messages, response,
                items=messages_list,  # Pass pre-drained items directly
                factory=self._make_user_message,
            )

            # Re-acquire concurrency slot after waking from SLEEPING
            self._acquire_slot_with_logging(instance, "after_message_wakeup")
            if self._is_terminal_stop(inst_name):
                return SleepAction.BREAK_LOOP, None

            # Restore KV cache after slot acquired, before resuming execution —
            # gated on actual slot ownership and targeting the held endpoint only
            # (eviction safety; _slot_release is None for unlimited/no-slot endpoints).
            if instance._slot_release is not None:
                self._restore_held_slot_state(instance, inst_name)

            # Compression-halt after wakeup: proceed to main loop (Site 3 will wait if needed)

            return SleepAction.CONTINUE_LOOP, None

        elif self.pool.has_pending(inst_name):
            # Still waiting for background tools — user messages now wake sleeping agents too.
            current_time = time.monotonic()
            sleeping_duration = 0.0
            if instance.sleeping_since is not None:
                sleeping_duration = current_time - instance.sleeping_since

            # ── SLEEPING cap (Fix A1): force completion if waiting too long ──
            # A hung background tool must not hold the agent in SLEEPING forever.
            # On expiry, mirror the COMPLETING-transition pattern below exactly:
            # take the state lock, bail out if TERMINATED, transition to COMPLETING,
            # clear sleeping_since, then break the run() loop so post-loop cleanup
            # (state finalization, slot release) runs in run()'s finally block.
            if AGENT_SLEEPING_MAX_WAIT_SECONDS > 0 and \
                    sleeping_duration >= AGENT_SLEEPING_MAX_WAIT_SECONDS:
                logger.error(
                    f"SLEEPING timeout for {inst_name}: waited "
                    f"{int(sleeping_duration)}s (max "
                    f"{AGENT_SLEEPING_MAX_WAIT_SECONDS}s). Forcing COMPLETING."
                )
                with instance._state_lock:
                    if instance.state == AgentState.TERMINATED:
                        return SleepAction.BREAK_LOOP, None
                    instance._transition(AgentState.COMPLETING)
                    instance.sleeping_since = None
                # Enqueue an informational message. The agent is exiting the run()
                # loop (BREAK_LOOP), so this won't be re-processed by its own loop;
                # it remains in the queue as a log artifact / debugging context.
                self.pool.enqueue_message(
                    inst_name,
                    f"[SYSTEM] SLEEPING timeout: pending background tools did not "
                    f"complete within {AGENT_SLEEPING_MAX_WAIT_SECONDS}s "
                    f"(waited {int(sleeping_duration)}s). Forcing completion."
                )
                return SleepAction.BREAK_LOOP, None

            # Get settings with defaults
            wakeup_interval = getattr(self.pool.settings, 'sleeping_wakeup_interval', 5.0)

            # Log wakeup message periodically
            if (current_time - instance._last_wakeup_log) >= wakeup_interval:
                logger.info("SLEEPING - %s waiting %.1fs for background tools",
                            inst_name, sleeping_duration)
                instance._last_wakeup_log = current_time

            try:
                if (current_time - instance._last_waiting_debug_log) >= 5.0:
                    logger.debug("WAITING for background tools - %s (%.1fs)", inst_name, sleeping_duration)
                    instance._last_waiting_debug_log = current_time
            except Exception:
                # Debug log throttling must never crash the agent loop
                pass
            # Note: debug log throttling is safe without a lock since each
            # agent instance is only executed by one thread at a time.
            # Yield empty list signals waiting state without consuming turn
            return SleepAction.CONTINUE_LOOP, []

        else:
            # No pending tools and no messages — check for any late-arriving messages
            # via stable-state drain before transitioning to COMPLETING
            results_found = False
            while self._drain_and_inject(
                instance, inst_name, messages, llm_messages, response,
                drain_fn=self.pool.drain_queue,
                factory=self._make_user_message,
            ):
                results_found = True

            # Final safety drain — catches race conditions
            if self._drain_and_inject(
                instance, inst_name, messages, llm_messages, response,
                drain_fn=self.pool.drain_queue,
                factory=self._make_user_message,
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
                self._acquire_slot_with_logging(instance, "after_stable_drain")

                # Restore KV cache after slot acquired, before resuming execution —
                # gated on actual slot ownership and targeting the held endpoint only
                # (eviction safety; _slot_release is None for unlimited/no-slot endpoints).
                if instance._slot_release is not None:
                    self._restore_held_slot_state(instance, inst_name)

                # Exit if stopped after re-acquiring slot in sleep loop
                if self._is_terminal_stop(inst_name):
                    logger.debug(
                        f"[SLOT_STOP_CHECK] Terminal stop after stable drain for {inst_name}, exiting"
                    )
                    return SleepAction.BREAK_LOOP, None  # Stop detected — slot released in finally
                # Compression-halt: proceed to main loop (Site 3 will wait if needed)

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

        # Scan for {USE_CACHED_ENTRY_N} patterns using shared function (avoids
        # regex recompilation + code duplication)
        inst = self.pool.get_instance(instance_name)
        cache_pool = getattr(inst, 'cache_pool', None) if inst else None
        cached_refs = resolve_cached_entry_refs(parsed, cache_pool)

        # Always deep-copy for consistency (same path whether placeholders
        # exist or not)
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

        # BUG FIX (Bug 2): Extract log_file from args and pass through the
        # chain
        log_file = args.get('log_file')

        # Phase 4.1: Delegate to lifecycle manager for instance creation/reuse
        inst, is_reuse, session_was_loaded = self.lifecycle.find_or_create_instance(
            agent_class, instance_name, caller, nest_depth, force_fresh, log_file=log_file
        )

        # ── System message + skills handling (todo.md:115 fix) ───────────────
        # load_skill applies only to NEW instances / external loads. On recall of an
        # existing idle agent we keep conversation[0] verbatim and ignore load_skill,
        # so the system prompt is never mutated on recall (preserves prefix cache).
        # A recall must NOT be an external load (log_file restore): that returns
        # is_reuse=True AND session_was_loaded=True and still needs build + skill injection.
        _is_recall = is_reuse and not session_was_loaded and bool(inst.conversation) \
            and getattr(inst.conversation[0], 'role', None) == SYSTEM
        task_text = args.get('task', '')
        skill_manager = getattr(self.pool, 'skill_manager', None)
        context_text = args.get('context', '')   # only used in else branch for resolve_load_skill
        if _is_recall:
            # Reuse path: preserve existing system message byte-for-byte. Pass the
            # EXISTING conversation[0] as sys_msg so initialize_conversation's
            # in-place edit becomes a content no-op (same object/content). No skill
            # resolution or injection happens here — load_skill is ignored by design.
            sys_msg = inst.conversation[0]
            logger.debug(
                "[SKILLS] Recall of %s: preserving existing system message; "
                "skipping rebuild + skill injection (load_skill ignored on recall)",
                instance_name,
            )
        else:
            # New instance OR external load (session restored from log_file) OR a
            # defensive fallback when a reused instance has no valid system message.
            # Build a fresh system message and resolve/inject skills.
            # Use inst.agent_class (may differ from caller's agent_class if session
            # was loaded from log file)
            sys_msg = self.lifecycle.build_system_message(inst.agent_class, instance_name)

            # ── Skills System: Resolve load_skill and inject into sys_msg ─────
            # (task_text / context_text / skill_manager hoisted above the branch)

            # GLOBAL "Enable skills" setting (UI toggle → pool.settings.
            # default_load_skill_mode: 'AUTO'=ON, 'NONE'=OFF). This is the single
            # source of truth for whether Self-Augmentation is present.
            global_skills_enabled = \
                getattr(self.pool.settings, 'default_load_skill_mode', DEFAULT_LOAD_SKILL_MODE) != LOAD_SKILL_NONE

            # Per-call load_skill controls ONLY which MATCHED/auto skills load.
            # It never removes Self-Augmentation while the global toggle is ON.
            load_skill_value = args.get('load_skill')
            if load_skill_value is None:
                # No explicit per-call arg → fall back to the global setting.
                load_skill_value = getattr(self.pool.settings, 'default_load_skill_mode', DEFAULT_LOAD_SKILL_MODE)

            loaded_skills = []
            if skill_manager and global_skills_enabled:
                # (1) Matched/auto skills — gated by the PER-CALL load_skill arg.
                #     NONE → no matched skills; otherwise resolve as before.
                if isinstance(load_skill_value, str):
                    load_skill_mode_upper = load_skill_value.strip().upper()
                else:
                    load_skill_mode_upper = "AUTO"
                if load_skill_mode_upper != LOAD_SKILL_NONE:
                    try:
                        loaded_skills = skill_manager.resolve_load_skill(
                            load_skill_value, task_text, context_text
                        )
                    except Exception as e:
                        logger.warning("[SKILLS] Failed to resolve skills for %s: %s", instance_name, e)
                        loaded_skills = []

                # (2) Self-Augmentation — gated by the GLOBAL "Enable skills" toggle
                #     (global_skills_enabled), INDEPENDENT of the per-call load_skill arg.
                #     It is the meta-skill that enables runtime discovery and must be
                #     present whenever skills are globally ON, even if this call passed
                #     load_skill="NONE". _inject_skills_to_system_message is idempotent
                #     (skips when '## Active Skills' already exists).
                self_augmentation_instructions = skill_manager.load_full_instructions("self-augmentation")
                if self_augmentation_instructions and self_augmentation_instructions not in loaded_skills:
                    loaded_skills.append(self_augmentation_instructions)

            # Inject skills into system message using general-purpose helper.
            # When the global toggle is OFF (global_skills_enabled False),
            # loaded_skills stays empty → nothing is injected.
            _inject_skills_to_system_message(self.pool, sys_msg, loaded_skills if loaded_skills else None)

        # Build task message using lifecycle manager
        task_msg = self.lifecycle.build_task_message(args, caller, agent_class=inst.agent_class)

        # Phase 4.1: Delegate to lifecycle manager for conversation
        # initialization
        conv = self.lifecycle.initialize_conversation(
            inst, sys_msg, task_msg, is_reuse, instance_name, inst.agent_class, from_external_load=session_was_loaded
        )

        # Phase 4.1: Delegate to lifecycle manager for settings propagation
        self.lifecycle.propagate_settings(inst, caller, inst.agent_class, call_agent_args=args)

        # Track in active stack with depth info (thread-safe via RLock)
        with self.pool._execution._state_lock:
            self.pool._execution.active_stack.append((instance_name, inst._nest_depth))

        # Item 12: Initialize sub-agent WebUI state before execution begins
        # (Fix
        # Issue Y2: Use shared helper method instead of duplicated logic
        self._update_webui_state(instance_name, inst.agent_class, inst, conv, final_resp=[], is_initial=True)

        # Phase 4.4: Delegate to StreamPublisher for WebSocket push
        self.stream_publisher.push_initial_state(inst, caller)

        try:
            # Telemetry: track sub-agent call latency (non-blocking)
            _call_start = time.perf_counter()

            # Execute through unified loop — push stream_update events so the
            # frontend sees sub-agent tab updates independently of main agent
            # flow.
            # Without this, the main streaming loop is blocked during tool
            # execution
            # and no WebSocket events arrive until the sub-agent finishes.
            logger.debug("starting engine.run() for %s", instance_name)

            final_resp = []
            _update_counter = 0
            _last_sub_send = 0.0
            _sub_send_interval = 0.15  # Match main loop throttle (run_agent_unified.py line 154)

            # Bug
            if self.pool.is_instance_terminated(instance_name):
                logger.info("instance %s terminated before execution - skipping", instance_name)
                # Clear leftover queued messages to prevent accumulation
                q = self.pool.message_queues.get(instance_name)
                if q:
                    q.clear()
                return inst, []

            # Track cumulative tool calls across all turns
            total_tool_calls = 0

            # Bind the generator so we can close it deterministically on early
            # break (same pattern as core.py:691-692). Without close(), a break
            # on terminal stop leaves run() suspended before its exit finally,
            # so the child stays RUNNING and any re-entry trips the L1 guard.
            _run_gen = self.run(inst)
            try:
                for resp in _run_gen:
                    # Inner run() loop handles compression-halt via cooperative wait at Site 3.
                    # Only break here on terminal stops (which cause run() to yield final state and end).
                    if self._is_terminal_stop(instance_name):
                        break

                    # FIX BOOL_LEAK: Unpack (messages, is_streaming) tuple from
                    # engine.run()
                    # engine.run() yields tuples like (List[Message], bool), but we
                    # only need the message list
                    if isinstance(resp, tuple) and len(resp) == 2:
                        final_resp = resp[0]  # Extract just the message list
                    else:
                        final_resp = resp

                    # Count tool calls from FUNCTION role messages
                    total_tool_calls += sum(1 for m in final_resp if msg_field(m, 'role', '') == FUNCTION)

                    # Item 12: Throttled sub-agent WebUI state update (every 5
                    # turns) — Fix
                    _update_counter += 1
                    if _update_counter % 5 == 0:
                        # Issue Y2: Use shared helper method instead of duplicated
                        # logic
                        current_conv = list(inst.conversation) if hasattr(inst, 'conversation') else conv
                        self._update_webui_state(instance_name, inst.agent_class, inst, current_conv, final_resp)

                    # ── Push stream_update to frontend during sub-agent execution
                    # ──
                    # This is the key fix: without this, the main agent's streaming
                    # loop
                    # is blocked and no WebSocket events reach the frontend. The
                    # frontend
                    # relies on stream_update to call renderSubAgents() every
                    # ~200ms.
                    now = time.time()  # Use time.time() for consistency with run_agent_unified.py:135
                    if now - _last_sub_send >= _sub_send_interval:
                        self.stream_publisher.push_periodic_update(caller)
                        _last_sub_send = now
            finally:
                # Deterministic generator cleanup: close() forces the suspended
                # run() generator to unwind its exit finally (RUNNING→IDLE), so
                # the child instance is never left in RUNNING state. Idempotent.
                # Guard with hasattr: engine.run() may be mocked in tests to return a
                # plain iterator (no .close()); only real generators need closing.
                try:
                    if hasattr(_run_gen, 'close'):
                        _run_gen.close()
                except RuntimeError:
                    pass  # Already closed/exhausted

            # FIX MSG_COUNT_BUG: Removed conv.extend(final_resp) to prevent
            # duplicate messages.
            # Messages are already added to instance.conversation during
            # engine.run() via _process_response().
            # Note: For new instances, rebuild_conversation() creates a copy of
            # conv, so they are NOT the same
            # reference — only reused instances share the same list. Extending
            # again would cause duplication
            # regardless. See: .agent_lessons/lessons_msg_count_bug.md for
            # detailed analysis.
            self._create_completed = True  # Mark for finally-block EXIT log reason tracking

            # Unified auto-skill gating: both toggles must be ON, using pool settings as single source of truth
            from agent_cascade.auto_skill_helpers import run_auto_skill_proposal
            created_skills = run_auto_skill_proposal(
                pool=self.pool,
                skill_manager=skill_manager,
                inst=inst,
                task_text=task_text,
                instance_name=instance_name,
                total_tool_calls=total_tool_calls,
                append_fn=lambda msg: self._append_and_log(inst, self._make_user_message(msg)),
                rollback_fn=lambda pop_count: self.pool._rollback_instance(instance_name, pop_count=pop_count),
                is_stopped=lambda: self._is_terminal_stop(instance_name),
                engine_run_generator=lambda: self.run(inst),
            )

            # Item 12: Always emit final sub-agent state after loop completes
            # (Fix
            # Ensures even short-lived agents (<5 turns) appear in the WebUI
            # Issue Y2: Use shared helper method instead of duplicated logic
            current_conv = list(inst.conversation) if hasattr(inst, 'conversation') else conv
            self._update_webui_state(instance_name, inst.agent_class, inst, current_conv, final_resp)

            # ── Push final stream_update after sub-agent completes ──
            self.stream_publisher.push_final_state(inst, caller)

        finally:
            # Telemetry: record agent instance call (non-blocking, fires in
            # finally so failed delegations are counted too)
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

        # FIX: Return a copy of the actual instance conversation, not the stale
        # `conv` variable.
        # For new instances, rebuild_conversation() creates a COPY of conv via
        # list(new_messages),
        # so the original `conv` from initialize_conversation never receives
        # appended messages.
        # For reused instances, they happen to be the same reference — but
        # using inst.conversation
        # is correct in both cases. See: investigation report for sub-agent
        # response propagation bug.
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
        # FIX

        # Use lifecycle manager with force_fresh=True for system agents
        inst, is_reuse, session_was_loaded = self.lifecycle.find_or_create_instance(
            agent_class, instance_name, caller, nest_depth=0, force_fresh=True
        )

        # Build system message using lifecycle manager (use inst.agent_class
        # for consistency)
        sys_msg = self.lifecycle.build_system_message(inst.agent_class, instance_name)

        # Build task message directly — no image scanning for system-invoked agents.
        # build_task_message() scans caller's conversation for images referenced in
        # task text; system tasks don't reference caller images so this would be
        # wasteful and could accidentally match substrings. Construct plain task message.
        caller_prefix = f"This is a message from {caller}."
        if context:
            context_text = f"{caller_prefix}\n{context}"
        else:
            context_text = caller_prefix
        formatted_task = f'Context: {context_text}\n\nTask: {task}\n\nPlease help with this task.'
        task_msg = Message(role=USER, content=formatted_task)

        # Initialize conversation using lifecycle manager (pass actual is_reuse
        # value)
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
            # Build lightweight summary of latest message (or empty for initial
            # state)
            latest_summary = ''
            if not is_initial and final_resp:
                last_msg = final_resp[-1]
                role = msg_field(last_msg, 'role', '')
                content = msg_field(last_msg, 'content', '')
                # For FUNCTION role (tool results), include tool name in
                # summary
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
        """Calculate total tokens in a message list (with caching — Fix #2).
        
        Uses get_message_stats() for consistent token counting including chat
        template overhead. This aligns with llm/base.py::_count_tokens and ensures
        all code paths report the same estimates that match llama.cpp's actual counts.
        """
        try:
            # Check cache: if conversation length hasn't changed, reuse the
            # cached count
            inst = instance or getattr(self, '_current_instance', None)  # Prefer explicit param (thread-safe)
            if inst and inst._last_token_count_conversation_length >= 0 and len(messages) == inst._last_token_count_conversation_length:
                return inst._cached_token_count

            total = 0
            for msg in messages:
                stats = get_message_stats(msg)
                total += stats['tokens']

            # Update cache
            if inst:
                inst._cached_token_count = total
                inst._last_token_count_conversation_length = len(messages)

            return total
        except Exception as e:
            logger.debug(f"Token counting failed (using rough estimate): {e}")
            # Fallback: rough estimate (4 chars per token), including
            # reasoning_content
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

    # ── _detect_loop removed — now uses canonical detect_loop from
    # loop_detection.py ──

    def _detect_tool(self, message: Message) -> Tuple[bool, str, Any, str]:
        """Detect if a message contains a tool call. Returns (use_tool, tool_name, tool_args, text).

        Checks in order:
        1. function_call attribute (standard path)
        2. Embedded tool calls in content text (Qwen/PEG format leaked into content)

        NOTE: reasoning_content is intentionally NOT scanned. LLMs often leak
        tool call syntax into thinking/reasoning blocks as part of their
        chain-of-thought. Treating those as real tool calls causes infinite
        loops (detect → execute → LLM regenerates same reasoning → repeat).
        """
        func_call = (message.get('function_call') if isinstance(message, dict)
                     else getattr(message, 'function_call', None))
        text = (message.get('content', '') if isinstance(message, dict)
                else getattr(message, 'content', ''))

        if func_call:
            if isinstance(func_call, dict):
                return True, func_call.get('name'), func_call.get('arguments'), text
            else:
                return True, getattr(func_call, 'name', ''), getattr(func_call, 'arguments', ''), text

        # Scan content text for embedded tool calls as last resort
        if text:
            calls = _extract_tool_calls_from_text(text)
            if calls:
                return True, calls[0][0], calls[0][1], text

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
