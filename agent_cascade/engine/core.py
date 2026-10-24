"""ExecutionEngine — the composed engine class (Phase 1 module-split).

``ExecutionEngine`` is defined here as the composition of mixins that hold
the method clusters moved out of the original monolithic ``execution_engine.py``:

- :class:`~agent_cascade.engine.llm_call.LLMCallMixin`         — LLM-call cluster
- :class:`~agent_cascade.engine.auto_skill.AutoSkillMixin` — in-loop auto-skill trigger
- :class:`~agent_cascade.engine.compression_exec.CompressionExecMixin` — compression
- :class:`~agent_cascade.engine.tool_execution.ToolExecMixin`  — tool execution / images
- :class:`~agent_cascade.engine.memory_hint.MemoryHintMixin`   — memory-hint queries
- :class:`~agent_cascade.engine.token_budget.TokenBudgetMixin` — token budgeting
- :class:`~agent_cascade.engine.placeholder_cache.PlaceholderCacheMixin` — placeholder cache
- :class:`~agent_cascade.engine.slot_lifecycle.SlotLifecycleMixin` — slot lifecycle
- :class:`~agent_cascade.engine.sleep_wake.SleepWakeMixin`      — sleep/wake transitions
- :class:`~agent_cascade.engine.system_agent.SystemAgentMixin` — system-agent lifecycle

``__init__``, ``initialize`` and the core orchestration / state / streaming
methods remain in this class. All method bodies are moved VERBATIM from the
original file; only the mixin composition, module constants, and imports were added.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Tuple

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.compression.handler import CompressionHandler
from agent_cascade.lifecycle_manager import AgentLifecycleManager
from agent_cascade.llm.schema import USER, Message
from agent_cascade.log import logger
from agent_cascade.stream_publisher import StreamPublisher
from agent_cascade.tool_dispatcher import ToolDispatcher
from agent_cascade.utils.utils import msg_field, msg_set

# ── Constants (core) ───────────────────────────────────────────────────────────

# REACQUIRE_TIMEOUT relocated to settings.py (plan §3.4.1) so router.py's sticky-slot sync can
# share the same bound without a circular import. Imported below with the other settings.


# MAX_TEXT_LENGTH_FOR_REGEX now lives in helpers.py (its true home — used by the
# helper functions there).
# SAMPLING_AND_LIMIT_KEYS lives in llm_call.py (used by _build_merged_cfg).

from agent_cascade.engine.auto_skill import AutoSkillMixin
from agent_cascade.engine.compression_exec import CompressionExecMixin
from agent_cascade.engine.helpers import (_extract_tool_calls_from_text)
from agent_cascade.engine.llm_call import LLMCallMixin
from agent_cascade.engine.memory_hint import MemoryHintMixin
from agent_cascade.engine.placeholder_cache import PlaceholderCacheMixin
from agent_cascade.engine.response_pipeline import ResponsePipelineMixin
from agent_cascade.engine.run_loop import RunLoopMixin
from agent_cascade.engine.slot_lifecycle import SlotLifecycleMixin
from agent_cascade.engine.sleep_wake import SleepWakeMixin
from agent_cascade.engine.system_agent import SystemAgentMixin
from agent_cascade.engine.turn_setup import TurnSetupMixin
from agent_cascade.engine.token_budget import TokenBudgetMixin
from agent_cascade.engine.tool_execution import ToolExecMixin


class ExecutionEngine(LLMCallMixin, CompressionExecMixin, ToolExecMixin, MemoryHintMixin, TokenBudgetMixin,
                      PlaceholderCacheMixin, SlotLifecycleMixin, SleepWakeMixin, SystemAgentMixin, TurnSetupMixin,
                      ResponsePipelineMixin, AutoSkillMixin, RunLoopMixin):
    """Core execution coordinator — delegates to specialized handlers.

    Responsibilities:
    - Main turn loop orchestration
    - Phase dispatch (setup → pre-check → LLM → process → post-check)
    - State machine transitions
    - Delegation to LifecycleManager, CompressionHandler, ToolDispatcher, StreamPublisher

    The class is still large and is being decomposed incrementally; see
    ``plans/engine_core_audit_2026-10-23.md`` for the current audit and split plan.

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

    def _consume_turn(self, instance, turns: int) -> int:
        """Consume one turn from *turns* (remaining budget). On the FIRST consumption of
        this run() (instance._turn_consumed not yet set), also record a user turn in
        telemetry. Returns the new remaining count."""
        if not getattr(instance, '_turn_consumed', False):
            instance._turn_consumed = True
            if (tel := self._telemetry()) is not None:
                try:
                    tel.record_user_turn(instance.instance_name)
                except Exception:
                    pass  # telemetry must never break the agent loop
            # Durable activity clock (research §15): advance the persisted cumulative
            # user-turn counter once per turn. Same guard as telemetry → fires exactly once
            # per run(). Best-effort — a skill-manager failure must never break the loop.
            sm = getattr(self.pool, 'skill_manager', None) if hasattr(self, 'pool') else None
            if sm is not None:
                try:
                    sm.bump_activity_turn()
                except Exception:
                    pass  # activity clock must never break the agent loop
        return turns - 1

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

    def _append_and_log_to_llm(self, instance, msg, llm_messages, *, lock_held=False) -> None:
        """Append msg to conversation + JSONL log + the LLM working view, exactly once.

        _append_and_log() already appends to instance._cached_llm_messages. The local
        `llm_messages` is normally the SAME object as _cached_llm_messages (see
        _setup_turn), so we must NOT append again unconditionally — that double-appends
        injected system messages and corrupts the LLM view on recall/rebuild. Only mirror
        into `llm_messages` when it has diverged from _cached_llm_messages (e.g. after a
        forced-compression consolidation rebuild), so every path is exactly-once.
        """
        self._append_and_log(instance, msg, lock_held=lock_held)
        if llm_messages is not instance._cached_llm_messages:
            llm_messages.append(msg)


    def _get_pool_setting(self, name, default=None):
        """Safely read a live pool setting (UI-editable without restart)."""
        settings = getattr(self.pool, 'settings', None)
        return getattr(settings, name, default) if settings else default


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
            drain_fn: Optional[Callable[[str],
                                        Any]] = None,  # Drain mode: callable that takes inst_name and returns data
            items: Optional[Any] = None,  # Items mode: already-drained data to inject
            factory: Optional[Callable[[Any], Message]] = None,  # Converts raw item → Message
            log_level: str = 'debug',  # Most injection points are debug-level
    ) -> bool:
        """Drain a queue/buffer and inject results as USER messages into all working lists.

        Messages are appended atomically to all working lists (messages, llm_messages,
        response, instance.conversation) under instance._compression_lock using the
        centralized append_message() API, ensuring no length mismatches between cached
        lists and conversation with automatic cache invalidation.

        Exactly one of drain_fn or items must be provided. When neither is supplied,
        ``drain_fn`` defaults to ``self.pool.drain_queue`` and ``factory`` defaults to
        ``self._make_user_message`` (the common injection path).

        Returns True if any messages were injected, False otherwise.
        """
        if factory is None:
            factory = self._make_user_message
        if drain_fn is None and items is None:
            drain_fn = self.pool.drain_queue

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
                    first_msg.content = self.compression_handler._drain_pending_into_user_message(
                        instance, first_msg.content)
                    # Also drain generic tool warnings into USER messages
                    # (appended)
                    first_msg.content = self.compression_handler._drain_tool_warnings(instance,
                                                                                      first_msg.content,
                                                                                      prepend=False)
                    # Also drain cache notifications into USER messages
                    # (prepended)
                    first_msg.content = self.compression_handler._drain_cache_notifications(instance,
                                                                                            first_msg.content,
                                                                                            prepend=True)
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
            self._tail_sync_conversation(instance, inst_name, 'drain_inject')
        except Exception as e:
            logger.debug(f"Logging failed for {inst_name} (non-critical): {e}")

        # Proactive compression check after async child results are injected
        if processed_messages:
            self._proactive_compression_check(instance, messages, llm_messages, response, check_label='async-drain')

        return True


    @staticmethod
    def _make_user_message(text: str) -> Message:
        """Create a USER message from raw text."""
        return Message(role=USER, content=text)


    # ═══════════════════════════════════════════════════════════════════════
    #  Helper methods — tool detection, truncation
    # ═══════════════════════════════════════════════════════════════════════

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
        func_call = (message.get('function_call') if isinstance(message, dict) else getattr(
            message, 'function_call', None))
        text = (message.get('content', '') if isinstance(message, dict) else getattr(message, 'content', ''))

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

    def _append_system_notification(self, messages: List[Message], guard_prefix: str, notification_text: str):
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
            has_notification = any((isinstance(item, dict) and guard_prefix in str(item.get('text', ''))) or
                                   (isinstance(item, str) and guard_prefix in item) for item in content)
            if not has_notification:
                content.append({'type': 'text', 'text': notification_text})
