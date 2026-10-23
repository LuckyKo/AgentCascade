"""ExecutionEngine — the composed engine class (Phase 1 module-split).

``ExecutionEngine`` is defined here as the composition of mixins that hold
the method clusters moved out of the original monolithic ``execution_engine.py``:

- :class:`~agent_cascade.engine.llm_call.LLMCallMixin`         — LLM-call cluster
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
from agent_cascade.llm.schema import ASSISTANT, USER, Message
from agent_cascade.log import logger
from agent_cascade.settings import (AUTO_SKILL_ENABLED, AUTO_SKILL_EXTRA_TURNS,
                                    AUTO_SKILL_MIN_TURNS, DEFAULT_LOAD_SKILL_MODE, LOAD_SKILL_NONE)
from agent_cascade.stream_publisher import StreamPublisher
from agent_cascade.tool_dispatcher import ToolDispatcher
from agent_cascade.utils.utils import extract_text_from_message, msg_field, msg_set

# ── Constants (core) ───────────────────────────────────────────────────────────

# REACQUIRE_TIMEOUT relocated to settings.py (plan §3.4.1) so router.py's sticky-slot sync can
# share the same bound without a circular import. Imported below with the other settings.





def _extract_instance_output(*args, **kwargs):
    """Lazy wrapper around compression.helpers.extract_instance_output.

    Used only by the auto-skill in-loop trigger's snapshot fallback (§2.3 step 2).
    Imported inside the call so module import time is unchanged.
    """
    from agent_cascade.compression.helpers import extract_instance_output
    return extract_instance_output(*args, **kwargs)




# MAX_TEXT_LENGTH_FOR_REGEX / MIN_OUTPUT_LENGTH now live in helpers.py (their true
# home — used by the helper functions there); re-imported below alongside helpers.
# SAMPLING_AND_LIMIT_KEYS lives in llm_call.py (used by _build_merged_cfg).

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
                      ResponsePipelineMixin, RunLoopMixin):
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

    def _auto_skill_gates_met(self, instance) -> bool:
        """Cheap, pre-LLM auto-skill trigger gates (settings + one-shot flag only).

        Returns True iff the in-loop reflection extension COULD fire this run. Used both to
        decide whether to skip last-turn tool-disable (prefix stability) and as the fast-path
        gate at the top of _try_auto_skill_extension, so the two can never drift.
        Deliberately excludes auto_skill_qualifies (skill-creator/prompt build) — that is a
        deeper qualification checked later and must not be re-run on every final turn.
        """
        skill_manager = getattr(self.pool, 'skill_manager', None)
        if skill_manager is None:
            return False
        settings = getattr(self.pool, 'settings', None)
        if not getattr(settings, 'auto_skill_enabled', AUTO_SKILL_ENABLED):
            return False
        if getattr(settings, 'default_load_skill_mode', DEFAULT_LOAD_SKILL_MODE) == LOAD_SKILL_NONE:
            return False
        min_turns = getattr(settings, 'auto_skill_min_turns', AUTO_SKILL_MIN_TURNS)
        if instance._current_turn <= min_turns:
            return False
        with instance._compression_lock:
            if getattr(instance, '_auto_skill_proposed', False):
                return False
        return True

    def _try_auto_skill_extension(self, instance, messages, llm_messages, loaded_skill_names=None) -> bool:
        """In-loop auto-skill trigger (replaces the post-run two-run helper).

        Called from run() at the NATURAL-COMPLETION point (Phase 5), i.e. right before
        the loop breaks when _post_turn_checks reports a natural end — an assistant turn
        with no tool call, no pending async work, and non-empty content.

        IMPORTANT: _post_turn_checks returns False via FOUR paths (terminal stop, terminal
        stop during the compression wait, pure-thinking stall, genuine completion) that all
        collapse into the same ``if not completed:`` in run(). The caller therefore gates this
        call on ``_is_genuine_completion(instance, response)`` so it is reached ONLY on genuine
        completion — a stopped or stalled agent never triggers skill reflection. This method
        itself does NOT re-check stop/stall; the gating is the caller's responsibility.

        When all gates pass it:
          1. snapshots the pre-reflection task output onto the instance — at natural
             end the final answer is already committed (per-iteration order: LLM →
             _process_response commits → _post_turn_checks), so this captures the
             just-completed final answer, not a previous turn's reply;
          2. injects the reflection prompt into BOTH instance.conversation
             (via _append_and_log) and the loop-local working sets (R3),
          3. sets the one-shot _auto_skill_proposed flag.

        Returns True on a successful trigger (caller then resets the loop budget);
        False otherwise. Best-effort: ANY exception is logged and returns False —
        a gate failure must never break the run.
        """
        try:
            # ── Gates (fast path, no locks) ────────────────────────────────
            # Same cheap gates as the final-turn tool-disable decision in run() — one
            # shared helper so the two can never drift.
            if not self._auto_skill_gates_met(instance):
                return False
            settings = getattr(self.pool, 'settings', None)
            skill_manager = self.pool.skill_manager
            # Live-read turn threshold (UI-editable, no restart). Shared by the gate above and
            # passed to auto_skill_qualifies so BOTH gates agree on the same live value.
            min_turns = getattr(settings, 'auto_skill_min_turns', AUTO_SKILL_MIN_TURNS)

            # ── Snapshot capture (under the compression lock) ────────────────
            # The one-shot _auto_skill_proposed flag was already checked in the
            # _auto_skill_gates_met fast-path above; it cannot change within this
            # synchronous call, so no re-check is needed here. The lock below is
            # still required for the instance state mutation (task_output).
            with instance._compression_lock:
                # Find the last ASSISTANT message with text content — same
                # dict/Message branching as the turn-limit notice in run() — and
                # extract its text. If none exists, fall back to the current
                # result on the pre-reflection conversation (may be a FUNCTION-role
                # warning or "no text output" string; that is the honest state of
                # this run). Never leave the attribute unset.
                snapshot = None
                for msg in reversed(instance.conversation):
                    msg_role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
                    if msg_role != ASSISTANT:
                        continue
                    has_text = False
                    if isinstance(msg, dict):
                        content = msg.get('content', '')
                        if isinstance(content, list):
                            has_text = any(isinstance(item, dict) and item.get('type') == 'text' for item in content)
                        elif isinstance(content, str):
                            has_text = bool(content)
                    else:
                        content = getattr(msg, 'content', '')
                        if isinstance(content, list):
                            has_text = any(isinstance(item, dict) and item.get('type') == 'text' for item in content)
                        elif isinstance(content, str):
                            has_text = bool(content)
                    if has_text:
                        snapshot = extract_text_from_message(msg, add_upload_info=False)
                        break
                if snapshot is None:
                    snapshot = _extract_instance_output(list(instance.conversation),
                                                        instance.instance_name,
                                                        pool=self.pool)

                instance._auto_skill_task_output = snapshot

            # ── Qualification + prompt build (pure; no conversation/flag mutation) ─
            prompt = skill_manager.auto_skill_qualifies(instance,
                                                        instance._current_turn,
                                                        loaded_skill_names=loaded_skill_names,
                                                        min_turns=min_turns)
            if not prompt:
                return False

            # ── Inject via the existing path (R3: all targets, same object) ────
            # _append_and_log_to_llm appends to conversation + JSONL log and mirrors
            # into llm_messages exactly once (identity-checked against
            # _cached_llm_messages). `messages` (full working set) is a separate list
            # object here — run() builds it from a fresh copy in _setup_turn — so it
            # still needs its own append for the reflection prompt to reach both views.
            user_msg = self._make_user_message(prompt)
            with instance._compression_lock:
                self._append_and_log_to_llm(instance, user_msg, llm_messages, lock_held=True)
                messages.append(user_msg)

            with instance._compression_lock:
                instance._auto_skill_proposed = True

            logger.info('[AUTO-SKILL] In-loop trigger fired for %s (turns=%d)', instance.instance_name,
                        instance._current_turn)
            return True
        except Exception as e:
            logger.warning('[AUTO-SKILL] In-loop trigger failed for %s: %s', getattr(instance, 'instance_name', '?'), e)
            return False

    def _get_pool_setting(self, name, default=None):
        """Safely read a live pool setting (UI-editable without restart)."""
        settings = getattr(self.pool, 'settings', None)
        return getattr(settings, name, default) if settings else default

    def _get_auto_skill_extra_turns(self) -> int:
        """Read the live auto-skill extra-turn budget with the module default."""
        return self._get_pool_setting('auto_skill_extra_turns', AUTO_SKILL_EXTRA_TURNS)

    def _grant_auto_skill_extension(self, instance) -> int:
        """Shared budget-reset side-effects for a FIRED auto-skill reflection extension.

        Called by BOTH trigger sites (the Phase-5 natural-completion path and the new
        Phase-4 last-turn-tool-call path) after _try_auto_skill_extension returns True.
        Snapshots the pre-trigger max_turns (R6 — restored in run()'s exit finally block)
        and extends instance.max_turns by AUTO_SKILL_EXTRA_TURNS from the
        current turn. Returns the new max_turns so the caller mirrors it into its loop-local
        `max_turns` and sets ``turns_available = AUTO_SKILL_EXTRA_TURNS`` +
        ``_suppress_budget_warnings = True`` (run() frame locals, as is the trailing
        ``yield response; continue``). Does NOT re-run trigger/qualification logic.
        """
        instance._auto_skill_orig_max_turns = instance.max_turns   # R6 snapshot
        extra_turns = self._get_auto_skill_extra_turns()
        new_max = instance._current_turn + extra_turns
        instance.max_turns = new_max
        return new_max

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
    #  Main Execution Loop — Core turn loop orchestration and phase dispatch
    # ═══════════════════════════════════════════════════════════════════════











    # ═══════════════════════════════════════════════════════════════════════
    #  Phase Methods — each ~20-60 lines, independently testable
    # ═══════════════════════════════════════════════════════════════════════























            # Clean thinking blocks from function call arguments (P4
            # continuation) — REMOVED: json_loads already strips pre-parse,
            # and per-value normalization is a corruption risk.

























    # ═══════════════════════════════════════════════════════════════════════
    #  Tool Execution — unified path for ALL tools including call_agent
    # ═══════════════════════════════════════════════════════════════════════




    # ═══════════════════════════════════════════════════════════════════════
    #  WebUI State Update Helpers (Issue Y2: Extract duplicated logic)
    # ═══════════════════════════════════════════════════════════════════════


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
