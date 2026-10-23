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
- :class:`~agent_cascade.engine.system_agent.SystemAgentMixin` — system-agent lifecycle

``__init__``, ``initialize`` and the core orchestration / state / streaming
methods remain in this class. All method bodies are moved VERBATIM from the
original file; only the mixin composition, module constants, and imports were added.
"""

from __future__ import annotations

import copy
import random
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator, List, Optional, Tuple, Union

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.compression.handler import CompressionHandler
from agent_cascade.exceptions import (AgentTerminatedError, CharacterRunDetected, ContextWindowExceeded,
                                      FallbackCompressionRequired, MaxTokenExceeded)
from agent_cascade.inner_loop_detect import InnerLoopDetector, save_loop_sample
from agent_cascade.lifecycle_manager import AgentLifecycleManager
from agent_cascade.llm.schema import ASSISTANT, FUNCTION, SYSTEM, USER, Message
from agent_cascade.log import logger
from agent_cascade.operation_manager import clear_current_instance_name, set_current_instance_name
from agent_cascade.retry_policy import RetryPolicy, calculate_backoff, classify_error
from agent_cascade.settings import (AGENT_SLEEPING_MAX_WAIT_SECONDS, AUTO_SKILL_ENABLED, AUTO_SKILL_EXTRA_TURNS,
                                    AUTO_SKILL_MIN_TURNS, CHARS_PER_TOKEN_ESTIMATE,
                                    COMPRESSION_RECOUNT_THRESHOLD,
                                    DEFAULT_LOAD_SKILL_MODE, DEFAULT_MAX_INPUT_TOKENS, DEFAULT_MAX_TURNS,
                                    DEFAULT_TOOL_RESULT_MAX_CHARS, LLM_MAX_RETRIES, LLM_RETRY_BASE_DELAY,
                                    LLM_RETRY_MAX_BACKOFF, LOAD_SKILL_AUTO, LOAD_SKILL_NONE, MAX_AUTO_CONTINUE_ATTEMPTS,
                                    REASONING_ONLY_CONTINUE_ATTEMPTS, SOFT_CONTINUE_NUDGE_ENABLED,
                                    SOFT_TURN_LIMIT, SOFT_TURN_LIMIT_ABSOLUTE_MAX, SOFT_TURN_LIMIT_WARNING_INTERVAL,
                                    STREAM_MAX_SILENCE_SECONDS, STREAM_MAX_TOTAL_SECONDS)
from agent_cascade.settings import InnerLoopSettings as _InnerLoopSettings
from agent_cascade.skills.selector import classify_completion
from agent_cascade.stream_publisher import StreamPublisher
from agent_cascade.tool_dispatcher import ToolDispatcher
from agent_cascade.tool_utils import (MAX_SPILL_SIZE, clear_truncation_state, generate_spillover_filename,
                                      mark_tool_call_truncated, was_tool_call_truncated)
from agent_cascade.utils.utils import extract_text_from_message, msg_field, msg_set

# ── Constants (core) ───────────────────────────────────────────────────────────
SLEEPING_LOOP_BACKOFF = 0.1  # Seconds to sleep when re-entering loop from SLEEPING state
_COMPRESSION_WAIT_TIMEOUT = 1.0  # Seconds to wait per iteration when suspended by compression

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
from agent_cascade.engine.helpers import (MAX_TEXT_LENGTH_FOR_REGEX, MIN_OUTPUT_LENGTH, SleepAction,
                                          _build_resources_block, _build_session_metadata, _check_message_truncation,
                                          _extract_text_content, _extract_tool_calls_from_text,
                                          _is_incomplete_state, _normalize_gemma_thought_tags,
                                          _normalize_thinking_blocks,
                                          _replace_resources_block, _replace_section)
from agent_cascade.engine.llm_call import LLMCallMixin
from agent_cascade.engine.memory_hint import MemoryHintMixin
from agent_cascade.engine.placeholder_cache import PlaceholderCacheMixin
from agent_cascade.engine.slot_lifecycle import SlotLifecycleMixin
from agent_cascade.engine.system_agent import SystemAgentMixin
from agent_cascade.engine.telegram_stream import _tg_push_final, _tg_stream_push
from agent_cascade.engine.token_budget import TokenBudgetMixin
from agent_cascade.engine.tool_execution import ToolExecMixin

def _last_assistant_text(turn_output: List[Message]) -> str:
    """Return the last assistant message's text content, ``''`` if there is none.

    Deliberately NOT reusing _is_incomplete_state: that walks the whole list to classify a
    shape, this only extracts the text the classifier needs to read.
    """
    for msg in reversed(turn_output):
        if msg_field(msg, 'role', '') != ASSISTANT:
            continue
        return _extract_text_content(msg)
    return ''


class ExecutionEngine(LLMCallMixin, CompressionExecMixin, ToolExecMixin, MemoryHintMixin, TokenBudgetMixin,
                      PlaceholderCacheMixin, SlotLifecycleMixin, SystemAgentMixin):
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
        extra_turns = self._get_pool_setting('auto_skill_extra_turns', AUTO_SKILL_EXTRA_TURNS)
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
    def _last_user_task_text(instance) -> Optional[str]:
        """Return the last USER-role message text in the instance conversation, or None.

        At run start this is the task the run is working on. It is the authoritative input
        for ``propose_skill``'s Tier-2 self-match gate (BUG_0123), because the proposal's own
        ``generated_from_task`` field is controlled by the model being screened.
        """
        conversation = getattr(instance, 'conversation', None)
        if not isinstance(conversation, (list, tuple)):
            return None
        # Engine-injected USER-role messages are not tasks: budget warnings, background/agent
        # results, shell result wrappers, compression markers and the reflection prompt.
        engine_prefixes = ('[SYSTEM WARNING:', '[SYSTEM]', '[BACKGROUND TOOL RESULT', '[Agent ',
                           '⟨shell_cmd', '--- CONTEXT COMPRESSED', '## Skill Reflection')
        for msg in reversed(conversation):
            role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
            if role != USER:
                continue
            try:
                text = extract_text_from_message(msg, add_upload_info=False)
            except Exception:  # pragma: no cover - defensive
                continue
            if not isinstance(text, str) or not text.strip():
                continue
            if text.lstrip().startswith(engine_prefixes):
                continue
            return text
        return None

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
        logger.debug('engine.run() ENTRY - instance=%s', instance.instance_name)
        # Every run starts with a clean turn-consumed flag so the first budget
        # consumption of this run counts as exactly one user turn in telemetry.
        # Set BEFORE any early-exit path so a stale flag can never suppress it.
        instance._turn_consumed = False
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
                raise RuntimeError(f"[BUG] {instance.instance_name} entered engine.run() in state "
                                   f"{instance.state.name} — should be IDLE. L1 race guard failed!")
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

        # Task provenance for propose_skill's Tier-2 self-match (BUG_0123). Capture it at run
        # START: the last USER message here is the task this run is working on, while later
        # engine notices and the reflection prompt are also USER-role messages and would be
        # picked up if the trigger captured it lazily.
        instance._auto_skill_source_task = self._last_user_task_text(instance)

        # Clear truncation state at the start of each agent turn to prevent
        # stale markers
        clear_truncation_state()
        instance._loop_rollback_count = 0

        # Initialize variables before try block to handle exceptions during
        # _setup_turn
        messages = None
        llm_messages = None
        response = None
        early_exit = False  # Set True on empty-conversation early exit so the finally block does not double-drain the queue.

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

        # Sticky slot plan change #7: a non-None _slot_release at run() entry means a
        # previous run leaked its permit without going through a release point. The
        # original implementation CLEARED it without releasing, which converted a
        # recoverable leak into a PERMANENT zombie holder (the SlotHolder stays in
        # pool._running, keyed by instance_name — only a release carrying the matching
        # acquisition_id can remove it, and that callback was just discarded).
        # Correct behavior: release it, then continue. The helper logs a loud
        # [SLOT_STALE_PERMIT] WARNING when it actually found one — reaching here is
        # always an upstream bug and must stay greppable.
        self._discard_stale_permit(instance, instance.instance_name,
                                   context='run() entry stale permit', action='drop-stale-guard')
        instance._compression_suspended_at = 0.0  # Reset per-run suspension marker (BUG-4/8 exit-finally)

        try:
            if self._is_terminal_stop(instance.instance_name):
                return  # Terminal stop — don't start work (exit finally → IDLE)

            self._acquire_slot_with_logging(instance, 'initial')

            # Exit if stopped after slot acquire — prevents stale slot reuse post-stop
            if self._is_terminal_stop(instance.instance_name):
                self._release_slot(instance, instance.instance_name, action='drop-exit')
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
                    sys_prompt = ''
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
                        model=model,
                        generate_cfg=cfg,
                        system_prompt=sys_prompt,
                        tools=tools_list,
                        api_base=api_base,
                    )
                    desc = tel.describe_config(model=model, generate_cfg=cfg, tools=tools_list, api_base=api_base)
                    tel.record_turn_start(instance.instance_name,
                                          config_fingerprint=fp,
                                          config_description=desc,
                                          agent_class=instance.agent_class)
                except Exception:
                    pass

            messages, llm_messages, response = self._setup_turn(instance)
            # _setup_turn's documented contract (see its docstring) is to return
            # (None, None, None) on the empty-conversation early exit, so guard the
            # debug log against None — calling len() here crashed the early-exit path.
            logger.debug(
                f"[TURN_DONE] Got messages={len(messages) if messages is not None else 'None'}, "
                f"llm_messages={len(llm_messages) if llm_messages is not None else 'None'}"
            )
            if not messages:
                # Safety: drain any queued user messages before exiting, so
                # they aren't lost.
                # Safe to append directly: on the re-entry path below we
                # re-run _setup_turn, which rebuilds from instance.conversation
                # under the compression lock and therefore sees these; on the
                # true early-exit path the turn is abandoned and the cached
                # lists are never used again.
                # This prevents reintroducing the silent cache rebuild bug from
                # Fix 1.
                inst_name = instance.instance_name
                queued = self.pool.drain_queue(inst_name)
                drained_count = 0
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
                        drained_count += 1
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
                        _check_tail(inst_name, conv, log_inst.log_path, context='early_exit')
                    except Exception:
                        pass  # Non-critical check

                # ── Empty-conversation re-entry (defense-in-depth) ────────────
                # If we drained real user messages into an empty conversation,
                # re-run _setup_turn so the LLM actually sees them this turn.
                # Without this, the early exit consumed the message and returned
                # without any LLM call — the user had to send a second message.
                # Upstream cause: run_agent_unified.py does not seed a system
                # message when none is available, so the conversation can be
                # empty on the first turn. Do not remove this as redundant —
                # it is the safety net for any empty-conversation early exit.
                if drained_count > 0:
                    logger.info(
                        '[EARLY_EXIT_REENTRY] %s: drained %d msg(s) into empty conversation, re-entering turn',
                        inst_name, drained_count,
                    )
                    messages, llm_messages, response = self._setup_turn(instance)

                if not messages:
                    # No re-entry (or re-entry still empty) — original early exit.
                    logger.debug('early exit - %s (_setup_turn returned empty)', inst_name)
                    # Turn-end telemetry is handled by the run() finally (BUG_0127),
                    # so every exit path is covered exactly once.
                    early_exit = True  # Queued messages were drained above; the exit
                                       # finally must NOT drain again (double-drain race).
                    return  # Manual command handled or error

            max_turns = instance.max_turns or DEFAULT_MAX_TURNS
            turns_available = max_turns
            # BUG_0140: number of turns available for THIS run, frozen at start. The
            # last-turn/turn-limit messages must only fire when the run had > 1 turn
            # available; max_turns is mutable (auto-skill reflection extends it), so
            # it cannot answer that question at injection time.
            initial_turns_available = turns_available
            # Soft turn limit (todo.md:107): snapshot the pool toggle for this run. When
            # ON, the loop may continue past max_turns while still emitting the normal
            # budget warnings; SOFT_TURN_LIMIT_ABSOLUTE_MAX is the safety ceiling (never
            # below the configured hard cap). Default OFF preserves the hard stop.
            soft_turn_limit = bool(self._get_pool_setting('soft_turn_limit', SOFT_TURN_LIMIT))
            soft_limit_absolute_cap = max(initial_turns_available, SOFT_TURN_LIMIT_ABSOLUTE_MAX)
            # Over-limit warning bookkeeping (only used when soft_turn_limit is ON).
            _soft_limit_last_warn_past = 0
            inst_name = instance.instance_name
            turns_90pct = max(2, int(max_turns * 0.1))  # 90% threshold, min 2 to avoid collision with final turn
            turns_50pct = max(3, int(max_turns * 0.5))  # 50% mid-point warning, min 3 to avoid overlap with 90%/final
            # Start False; set True during the auto-skill reflection extension (Phase 5) to
            # suppress the 50%/90% budget warnings for the fresh reflection turns (reset block below).
            _suppress_budget_warnings = False

            while turns_available > 0 or (soft_turn_limit and
                                          max_turns - turns_available < soft_limit_absolute_cap):
                # Track current turn on instance for system_info tool access
                instance._current_turn = max_turns - turns_available + 1

                # Flag to track if we disabled tools this iteration (for safe
                # cleanup)
                final_turn_tools_disabled = False

                # ── SLEEPING STATE GUARD ────────────────────────────────────
                # Agents wake on ANY queued message (user messages or async tool results).
                # Both types now use the same message_queue; unified wakeup simplifies flow.
                if instance.state == AgentState.SLEEPING:
                    action, yield_value = self._handle_sleeping_state(instance, messages, llm_messages, response)
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
                # Pattern: _make_user_message → _append_and_log_to_llm, which appends to
                # conversation + JSONL log and mirrors into llm_messages exactly once.
                # NOTE: these are NOT added to the local `messages` list because _setup_turn()
                # runs ONCE per run (not per iteration); loop detection/compression thus see a view
                # that omits them — same as the final-turn warning. Bounded (<=2 messages) and does
                # not affect LLM context, which uses llm_messages.
                if not _suppress_budget_warnings and turns_available == turns_50pct:
                    warn_msg = (f"[SYSTEM WARNING: Halfway through your turn budget. "
                                f"You have {turns_available} turn(s) remaining out of {max_turns} total. "
                                f"Assess your progress and plan remaining steps.]")
                    warn_user = self._make_user_message(warn_msg)
                    self._append_and_log_to_llm(instance, warn_user, llm_messages)
                if not _suppress_budget_warnings and turns_available == turns_90pct:
                    warn_msg = (f"[SYSTEM WARNING: Turn limit approaching. "
                                f"You have {turns_available} turn(s) remaining out of {max_turns} total. "
                                f"Plan your remaining steps carefully.]")
                    warn_user = self._make_user_message(warn_msg)
                    self._append_and_log_to_llm(instance, warn_user, llm_messages)
                if turns_available == 1:
                    # If the auto-skill reflection extension is going to fire, keep tools enabled:
                    # disabling them changes the request function schema -> KV full reprocess, and
                    # the extension would force a second one on the first reflection turn. Keeping
                    # tools on keeps the prefix stable so this "last turn" behaves like a normal
                    # mid-run turn. (The trigger itself fires later at Phase 5; these cheap gates
                    # are fully knowable here, before the LLM call.)
                    _auto_skill_will_extend = self._auto_skill_gates_met(instance)

                    # Final-turn handling for a run that is genuinely ending on its last
                    # turn. The auto-skill reflection trigger no longer lives here — it
                    # now fires at the natural-completion point (Phase 5, below), which is
                    # reached only when _post_turn_checks reports completion. So this block
                    # always applies to non-extended runs: warn and disable tools so the
                    # agent is forced to deliver a final answer on its last turn.
                    # Final turn warning: insert as a separate user message
                    # (not inline) so it's treated as a distinct conversational
                    # turn, not appended to the last message.
                    # Muted for single-turn runs: this is their only turn and
                    # tools are being disabled below regardless, so the warning
                    # adds no information — just noise in the transcript.
                    # BUG_0140: gate on the run-start turn count, not max_turns
                    # (which auto-skill reflection may have extended mid-run).
                    # Change 1 (todo149): always warn on the final turn regardless of
                    # _auto_skill_will_extend — when gates pass a reflection extension may be
                    # granted this turn, and hiding the "you are out of turns" signal would be
                    # misleading. The tool-disable gate below (core.py:909) is UNCHANGED, so
                    # tools stay enabled in the gates-pass case; the "tools have been disabled"
                    # clause is then slightly inaccurate but acceptable (all assertions match on
                    # the '[SYSTEM WARNING: Final turn' prefix).
                    if initial_turns_available > 1:
                        if soft_turn_limit:
                            final_msg = self._make_user_message(
                                '[SYSTEM WARNING: Final turn. You have 1 turn left before the '
                                'soft turn limit; tools stay enabled and the run will continue '
                                'past the limit. Wrap up and deliver your results now.]')
                        else:
                            final_msg = self._make_user_message(
                                f"[SYSTEM WARNING: Final turn. You have 1 turn left to complete your task and tools have been disabled. "
                                f"Wrap up and deliver your results now.]")
                        self._append_and_log_to_llm(instance, final_msg, llm_messages)

                    # Disable ALL tools on the last turn so agent is forced to
                    # return a final answer. Soft turn-limit mode deliberately keeps
                    # tools enabled — forcing a text-only answer here would stop the
                    # run at the hard cap, which is exactly what the toggle disables.
                    if not _auto_skill_will_extend and not soft_turn_limit:
                        template = self.pool.get_template(instance.agent_class)
                        if template and hasattr(template, 'function_map'):
                            all_tools = list(template.function_map.keys())
                            if all_tools:
                                if not hasattr(instance,
                                               '_generate_cfg_override') or instance._generate_cfg_override is None:
                                    instance._generate_cfg_override = {}
                                instance._generate_cfg_override['disabled_tools'] = all_tools
                                final_turn_tools_disabled = True

                # Soft turn limit: keep nudging the agent every N turns past max_turns.
                # `turns_past_limit` counts the turn currently about to run: the first
                # over-budget turn is 1, the fifth is 5, etc. Using `last_warn_past`
                # (instead of exact modulo) guarantees a warning even if a pre-LLM
                # real-cycle check consumes several turns before this block runs.
                if soft_turn_limit and turns_available <= 0:
                    turns_past_limit = 1 - turns_available
                    if turns_past_limit - _soft_limit_last_warn_past >= SOFT_TURN_LIMIT_WARNING_INTERVAL:
                        over_msg = self._make_user_message(
                            f"[SYSTEM WARNING: You are over the turn limit, please wrap up. "
                            f"{turns_past_limit} turn(s) past the {max_turns}-turn limit.]")
                        self._append_and_log_to_llm(instance, over_msg, llm_messages)
                        _soft_limit_last_warn_past = turns_past_limit
                elif turns_available > 0:
                    # A fresh budget (including an auto-skill reflection reset) gets its
                    # own 5-turn overage cadence.
                    _soft_limit_last_warn_past = 0

                # Change 2 gate signal (todo149): True exactly when turns_available was 1 at the
                # top of this iteration (i.e. this is the last turn of the ORIGINAL budget).
                # Computed BEFORE _consume_turn so it reflects the pre-decrement value. It can
                # never be true mid-run (mid-run turns have turns_available > 1 here). After an
                # extension resets turns_available to AUTO_SKILL_EXTRA_TURNS, a later `== 1` is
                # the REFLECTION's last turn — but by then _auto_skill_proposed is set, so
                # _auto_skill_gates_met returns False and it cannot re-fire (one-shot).
                _is_last_original_turn = (turns_available == 1)
                turns_available = self._consume_turn(instance, turns_available)

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
                            if (result := self._check_stream_termination(stream_tick, inst_name, response, turn_output,
                                                                         partial_msgs)) is not None:
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
                            is_retrying_msg = isinstance(content, str) and content.startswith('[RETRYING]')

                            stream_tick += 1
                            if (result := self._check_stream_termination(stream_tick, inst_name, response, turn_output,
                                                                         partial_msgs)) is not None:
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
                            logger.warning(
                                f"[MSG_VALIDATION] Skipping non-Message in LLM response for {instance.instance_name}: type={type(msg).__name__}, value={str(msg)[:100]}"
                            )
                finally:
                    gen.close()  # Ensure generator cleanup on early break (prevents resource leak)

                # Check generation change (old run superseded by newer one)
                # alongside stop
                if not terminated_during_stream:
                    if self._is_terminal_stop(instance.instance_name):
                        logger.debug('terminal stop - %s', instance.instance_name)
                        yield response
                        break
                    elif self._is_suspended_by_compression(instance.instance_name):
                        # Compression-halt is a suspension, not termination — wait cooperatively.
                        logger.debug('suspended by compression, waiting - %s', instance.instance_name)
                        if not self._wait_for_compression_to_clear(instance.instance_name):
                            yield response
                            break  # Terminal stop during wait
                        yield response
                        continue  # Resumed (compression done), continue the main loop with this turn's output

                # Clean up last-turn tool disabling only if we set it this
                # iteration
                if final_turn_tools_disabled:
                    if hasattr(instance, '_generate_cfg_override') and isinstance(instance._generate_cfg_override,
                                                                                  dict):
                        instance._generate_cfg_override.pop('disabled_tools', None)

                # ── Memory-Hint trigger (feature: memory_hint) ────────────────
                # Phase-3/Phase-4 boundary: turn_output is fully populated here. Best-effort;
                # never blocks, never mutates the turn budget. Hints ride the tool-warning
                # queue (NO user-message injection — plan §0 / R1).
                self._maybe_submit_memory_hint(instance, turn_output)

                # logger.debug(f"[LLM_DONE] {inst_name} got {len(turn_output)}
                # messages from LLM")
                # ── Phase 4: Response Processing and Tool Execution ─────────
                if self._process_response(instance, turn_output, messages, llm_messages, response):
                    # NOTE: auto-continue deliberately does NOT reset turns_available — the
                    # per-iteration decrement (turns_available -= 1) already ran before this
                    # LLM call, so every auto-continue consumes exactly one real turn and
                    # max_turns stays a hard budget.
                    # Change 2 (todo149): if THIS was the last turn of the original budget and the
                    # cheap gates pass, grant the auto-skill reflection extension INSTEAD of letting
                    # the loop exit on an exhausted budget. _process_response has ALREADY committed
                    # the assistant tool-call + FUNCTION result before returning, so the reflection
                    # prompt injected by _try_auto_skill_extension lands AFTER the FUNCTION message
                    # (order: assistant tool-call → FUNCTION result → user reflection-prompt). A
                    # tool-call turn `continue`s here and NEVER reaches Phase 5 in the same iteration,
                    # so this path and the natural-completion path cannot double-fire; the one-shot
                    # _auto_skill_proposed flag (checked inside _try_auto_skill_extension →
                    # _auto_skill_gates_met) also guarantees at most one extension per run. On a
                    # FAILED trigger (gates pass but auto_skill_qualifies returns no prompt) this
                    # condition is False and control falls through to the normal `yield; continue`
                    # below, so the loop exits on the exhausted budget with no flag poisoning.
                    if _is_last_original_turn and \
                            self._try_auto_skill_extension(
                                instance, messages, llm_messages,
                                loaded_skill_names=getattr(instance, '_loaded_skill_names', None)):
                        # Change 3 marker (todo149): the run did not end on a clean no-tool answer
                        # before reflecting → extract_instance_output must return the LAST reflection
                        # message, not the pre-reflection snapshot.
                        instance._auto_skill_dirty_stop = True
                        max_turns = self._grant_auto_skill_extension(instance)
                        _suppress_budget_warnings = True
                        turns_available = self._get_pool_setting('auto_skill_extra_turns', AUTO_SKILL_EXTRA_TURNS)
                        yield response
                        continue
                    # logger.debug("tool used - %s looping",
                    # instance.instance_name)
                    yield response
                    continue

                # ── Phase 5: Post-Turn Checks ───────────────────────────────
                completed = self._post_turn_checks(instance, messages, llm_messages, response)
                if not completed:
                    # _post_turn_checks breaks on four paths (stop ×2, stall, genuine
                    # completion); only the last may reflect. _is_genuine_completion
                    # excludes stop/stall so a stopped or stalled agent never triggers.
                    if self._is_genuine_completion(instance, response):
                        # TG-STREAM F2: push the final answer NOW, at stream time —
                        # this no-tool assistant turn IS the natural end, and its committed text
                        # lives in `response` (no history lookback). Observed BEFORE/independently
                        # of _try_auto_skill_extension: the old `and` short-circuit only fired when
                        # reflection qualified, so a run that completed without reflecting never
                        # pushed. Phase is keyed on instance._auto_skill_proposed — False here means
                        # 'pre' (this is the pre-extension answer); after the extension fires below,
                        # the reflection's own final turn reaches this same point with the flag set
                        # and pushes 'post'. See _tg_push_final for dedup/retry semantics.
                        _tg_push_final(instance, response, self.pool,
                                       phase='post' if getattr(instance, '_auto_skill_proposed', False) else 'pre')
                        if self._try_auto_skill_extension(
                                instance, messages, llm_messages,
                                loaded_skill_names=getattr(instance, '_loaded_skill_names', None)):
                            # Trigger fired (natural completion): grant AUTO_SKILL_EXTRA_TURNS fresh
                            # turns for the reflection. Instance-state mutations are shared with the
                            # Phase-4 tool-call path via _grant_auto_skill_extension; the loop-local
                            # reset + yield/continue stay here (run() frame locals).
                            max_turns = self._grant_auto_skill_extension(instance)
                            # 50%/90% warnings apply to the ORIGINAL budget, already exhausted here.
                            # The reflection is a fresh phase (turns_available counts down from
                            # AUTO_SKILL_EXTRA_TURNS while max_turns is extended), so those thresholds
                            # are meaningless and would print misleading "N remaining out of {extended}"
                            # messages. Suppress them; the final-turn warning (==1) still fires on the
                            # reflection's last turn.
                            _suppress_budget_warnings = True
                            # NOTE: this trigger fires at Phase 5 (natural completion), i.e. AFTER
                            # _consume_turn has already decremented turns_available for the triggering
                            # turn — so the reset is exactly AUTO_SKILL_EXTRA_TURNS (NOT +1). The old
                            # budget-exhaustion design fired BEFORE _consume_turn, which is why its
                            # reset carried a +1; that no longer applies at this call site.
                            turns_available = self._get_pool_setting('auto_skill_extra_turns', AUTO_SKILL_EXTRA_TURNS)
                            yield response
                            continue                                               # run the reflection turns
                    break

            # ── Cleanup: Turn limit reached ────────────────────────────────
            # Muted for single-turn runs: their single turn IS the complete task,
            # so there is no "incomplete" state — the notice would be pure noise.
            # BUG_0140: use the run-start turn count (max_turns may have been
            # extended mid-run by auto-skill reflection).
            # BUG_0141: in soft mode there is no max_turns hard stop, so this notice
            # is only correct when the absolute safety ceiling stopped the run.
            soft_limit_hard_stop = (
                soft_turn_limit and turns_available <= 0
                and (max_turns - turns_available) >= soft_limit_absolute_cap)
            if soft_limit_hard_stop:
                logger.warning('[SOFT_TURN_LIMIT] %s hit the absolute safety cap (%d turns)',
                               inst_name, soft_limit_absolute_cap)
            if turns_available <= 0 and ((not soft_turn_limit and initial_turns_available > 1)
                                         or soft_limit_hard_stop):
                # Inject turn limit notice into the LAST assistant message
                # content
                # instead of appending a new message. This ensures
                # extract_instance_output()
                # (which reads messages[-1]) returns the agent's actual output
                # with the warning.
                turn_notice = '\n\n[Turn limit reached — results may be incomplete. Continue if needed.]'
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

        except AgentTerminatedError:
            # Clean abort from termination during tool execution or slot acquire.
            # Don't log as error, don't yield error message — just exit cleanly.
            logger.debug(f"[DISMISSAL] Aborting run() for {instance.instance_name}: terminated")
        except Exception as e:
            # C4 fix: Catch unhandled exceptions — log and yield error state
            logger.error('EXCEPTION - %s: %s: %s', instance.instance_name, type(e).__name__, e, exc_info=True)
            error_msg = Message(role=ASSISTANT, content=f"[SYSTEM ERROR: {e}]")
            yield [error_msg]

        finally:
            # C4 fix: Always clean up — transition to IDLE regardless of how we
            # exit

            # ── Telemetry: close the turn exactly once, on every exit path ──────
            # normal completion, unhandled exception, AgentTerminatedError and
            # GeneratorExit (consumer abandoned the generator). Config/agent-class
            # attribution of every LLM/tool event created during the turn depends
            # on this pop, so skipping it makes the whole final turn invisible in
            # both tables, not just its duration (BUG_0127). record_turn_end is
            # idempotent (it pops), and is a no-op when run() exited via the two
            # pre-start terminal-stop returns above.
            if (tel := self._telemetry()) is not None:
                try:
                    tel.record_turn_end(instance.instance_name)
                except Exception:
                    pass

            # R6 (locked): restore the pre-trigger turn budget so a triggered run's
            # extended max_turns does NOT leak into this instance's NEXT run. The
            # snapshot is taken at trigger time (see _try_auto_skill_extension block);
            # when no trigger fired the attribute is absent and nothing happens.
            if getattr(instance, '_auto_skill_orig_max_turns', None) is not None:
                instance.max_turns = instance._auto_skill_orig_max_turns

            inst_name = instance.instance_name
            suspended_this_run = getattr(instance, '_compression_suspended_at', 0.0) > 0.0
            terminated = self._is_terminal_stop(inst_name)
            # TG-STREAM F3/D3: a terminal stop skips Phase 5 entirely, so the
            # final-answer push never fires — if the first-output push already went out but no
            # final one did, say so explicitly (the user got an early update, not the answer).
            if terminated and getattr(instance, '_tg_first_pushed', False) \
                    and getattr(instance, '_tg_final_pushed_phase', None) is None:
                logger.info('[TG-PUSH] run stopped before final push for %s (first-output push sent, '
                            'final answer not delivered)', inst_name)
            outstanding = self.pool.has_pending(inst_name) or self.pool.has_messages(inst_name)

            # BUG-8 FIX: preserve wakeups ONLY when a suspension-driven exit left real
            # work behind. Normal completions and terminal stops behave exactly as before.
            preserve = suspended_this_run and not terminated and outstanding

            # Early-exit (empty conversation) runs already drained the message queue
            # in their safety drain — draining again here would silently discard any
            # user message enqueued between the two drains (e.g. the first message of
            # a session after server restart). Skip the exit-finally cleanup entirely
            # for those runs: clear_pending is also unnecessary since no LLM call ran.
            if early_exit:
                logger.debug('early-exit drain already handled — skipping exit-finally queue drain for %s', inst_name)

            elif not preserve:
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
                        f"Content is in conversation; this is expected on early exit.")
                    instance._continue_saved_msg = None

            # Release concurrency slot on exit if still held (using helper
            # method FIX Mi3). Structured drop-exit event (change #10c).
            self._release_slot(instance, instance.instance_name, action='drop-exit')

            # FIX LogAppendFixer: Final sync to ensure all messages in
            # conversation are logged
            # This catches any injected messages that weren't followed by an
            # LLM call triggering _process_response() sync
            try:
                inst_name = instance.instance_name
                agent_class = instance.agent_class
                log_inst = self.pool.get_logger(inst_name, agent_class)

                already_logged_count = len(log_inst.data.get('history', []))
                with instance._compression_lock:
                    conv_len = len(instance.conversation)

                # Only sync if there's a mismatch (defensive check to avoid
                # redundant logging)
                if already_logged_count < conv_len:
                    logger.debug(
                        f"[FINAL_SYNC] {inst_name}: Catching up {conv_len - already_logged_count} unlogged messages "
                        f"(logged={already_logged_count}, conversation={conv_len})")
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
                            _check_tail(inst_name, conv, log_inst.log_path, context='final_sync')
            except Exception as e:
                logger.debug(
                    f"Final sync to JSONL failed for {getattr(instance, 'instance_name', 'unknown')} (non-critical): {e}"
                )

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
                    logger.debug('EXIT - %s %s→%s%s', instance.instance_name, current_state.name, target.name,
                                 ' [suspension-preserved]' if preserve else '')
                elif current_state == AgentState.TERMINATED:
                    logger.debug('EXIT - %s already TERMINATED', instance.instance_name)
                else:
                    logger.debug('EXIT - %s in %s state', instance.instance_name, current_state.name)

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
            Tuple of (messages, llm_messages, response), or (None, None, None)
            on error or when the conversation is empty (the early-exit path).
        """
        inst_name = instance.instance_name

        # Restore KV state ONLY when BOTH conditions hold:
        #   1. This instance currently HOLDS a concurrency slot (eviction safety — the
        #      FIFO guarantees single-holder for conc=0, so restoring while holding the
        #      slot can never evict another agent). We target the endpoint it actually
        #      holds, never the stale _last_endpoint_config (which may point at a shared
        #      conc=0 autoloader we no longer own; loading there would auto-evict a live
        #      sibling's resident model).
        #   2. A saved state label is pending (_state_label set) — i.e. an actual agent
        #      dispatch happened (sync or async) and a save ran, leaving a save file to
        #      consume. restore_instance_state() clears _state_label on success/failure,
        #      so it is set only between "save occurred" and "restore consumed it".
        #
        # The label gate is what stops us reloading on EVERY turn: without it, the slot
        # gate alone fires on every fresh user-message turn (run() re-acquires the slot
        # before _setup_turn), doing a 3.1GB disk read and — worse — a state/load that
        # destroys the warm KV cache, forcing an 85K-token full reprocess (~74s). On a
        # natural turn with no dispatch, _state_label is None → we skip the restore and
        # the slot's KV cache stays warm. Any resolution failure also skips the restore
        # (a missed restore is far better than a wrongful eviction).
        with instance._state_lock:
            has_saved_state = instance._state_label is not None
        if instance._slot_release is None:
            logger.debug('[STATE_RESTORE_SKIP] %s holds no slot — skipping state restore', inst_name)
            # Defense-in-depth: a label set without a held slot cannot be restored this turn.
            # Clear it so it does not leak and re-trigger an expensive state/load next turn.
            self._clear_orphaned_state_label(instance, inst_name)
        elif not has_saved_state:
            logger.debug('[STATE_RESTORE_SKIP] %s has no saved state to restore (no dispatch this turn) — '
                         'keeping warm KV cache', inst_name)
        else:
            self._restore_held_slot_state(instance, inst_name)

        with instance._compression_lock:
            conv = list(instance.conversation)

        if not conv:
            logger.warning('empty conversation for %s - early exit', inst_name)
            return None, None, None

        # Simple cache check: use cached working set if config hasn't changed
        can_use_cache = (instance._last_config_version == self.pool._config_version and instance._cached_messages and
                         instance._cached_llm_messages)

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
                        logger.debug(f"[CACHE_EXTEND] Extending cached working set for {inst_name} "
                                     f"by {current_len - cached_len} message(s)")
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
                        logger.info(f"[CACHE_MISMATCH] {inst_name}: conv={current_len}, cached={cached_len} "
                                    f"— forcing rebuild to resync")
                        can_use_cache = False

                if can_use_cache:
                    logger.debug(
                        f"[CACHE_HIT] Reusing cached messages={len(instance._cached_messages)}, llm_messages={len(instance._cached_llm_messages)}"
                    )
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

            # Frozen-prompt guard (todo 134): read under _compression_lock at the
            # point of use. RLock reentrancy is safe — freeze_system_prompt() acquires
            # the same lock on this thread later in this turn, and no other thread can
            # flip the flag between this read and the write below (only this thread's
            # own freeze call at the end of _setup_turn sets it).
            with instance._compression_lock:
                sys_prompt_frozen = instance._system_prompt_frozen
            if m0_role == SYSTEM and not sys_prompt_frozen:
                m0_content = m0.get('content', '') if isinstance(m0, dict) else getattr(m0, 'content', '')
                if isinstance(m0_content, str):
                    original_content = m0_content
                    # 1. Update identity "You are [instance]."
                    pattern = rf"(?i)You are\s+\w+\."
                    if re.search(pattern, m0_content):
                        m0_content = re.sub(pattern, f"You are {inst_name}.", m0_content, count=1)

                    # 2. Refresh Session Metadata section.
                    # Always a refresh (never first-injection) because the pre-log writers handle
                    # persistence for every instance class. _replace_section's byte-identical early-out
                    # makes this a no-op when unchanged, preserving the KV-cache prefix. Do NOT re-add
                    # a first-injection branch here: todo.md:155.
                    meta_block = _build_session_metadata(self.pool, instance)
                    if meta_block:
                        m0_content = _replace_section(m0_content, '## Session Metadata', meta_block)

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
                                escaped_old = re.escape('--- CURRENT AVAILABLE RESOURCES')
                                old_pattern = escaped_old + r'.*?(?=\n\n(?:#{1,6}|---)|\Z)'
                                m0_content = re.sub(old_pattern, '', m0_content, count=1, flags=re.DOTALL)

                            # First injection — insert before ## Active Skills if present,
                            # otherwise append at end. This ensures consistent order:
                            # identity/metadata → available agents → active skills
                            if '## Active Skills' in m0_content:
                                # Insert before the Active Skills section
                                m0_content = m0_content.replace('## Active Skills', new_block + '\n\n## Active Skills',
                                                                1)
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
                            diff_summary.append(
                                f"first_diff@{first_diff}: orig='{original_content[ctx_start:ctx_end]}' new='{m0_content[ctx_start:ctx_end]}'"
                            )

                        if isinstance(m0, dict):
                            m0['content'] = m0_content
                        else:
                            m0.content = m0_content
                        logger.debug(
                            f"[CACHE_REBUILD] System prompt content CHANGED for {inst_name} ({', '.join(diff_summary)})"
                        )

                        try:
                            log_inst = self.pool.get_logger(inst_name, instance.agent_class)
                            if log_inst.data['history']:
                                # Update in-memory system message only — do NOT rewrite the JSONL file here.
                                # data["history"] is the trimmed working set; rewriting the full-history file
                                # from it would destroy retained history. New text persists on next append.
                                log_inst.data['history'][0] = log_inst._format_message(m0)
                        except Exception as e:
                            logger.warning(f"Failed to update in-memory system message for {inst_name}: {e}")
                    else:
                        logger.debug(
                            f"[CACHE_REBUILD] System prompt for {inst_name} textually identical — skipping pool update")

        # messages = full working set; llm_messages = what actually goes to LLM
        # Apply slice to extract system + post-marker tail if markers exist
        sliced = self.pool.slice_history_for_llm(conv)
        llm_messages = list(sliced) if sliced else list(conv)

        # Sync caches — simple extend-or-rebuild model
        instance._cached_messages = conv
        instance._cached_llm_messages = llm_messages
        instance._last_config_version = self.pool._config_version

        # Freeze the system prompt at the end of the first successful rebuild.
        # The M1 block above produced the final prompt; from now on conversation[0]
        # is immutable for this instance's lifetime (reset_conversation unfreezes).
        # freeze_system_prompt() is idempotent (no-op if already frozen or conv[0]
        # is not SYSTEM), so calling it unconditionally is safe and avoids a
        # lock-free flag read.
        instance.freeze_system_prompt()

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
        """Check if this instance is suspended by a compression halt.

        Returns True only when the instance is tracked in ``pool._compression_halted`` —
        i.e. it was halted via the pool's public ``halt_all_instances()`` (manual /
        programmatic callers and tests). Forced compression itself no longer halts any
        sibling (t151b: the Compressor is a pure FIFO citizen bounded by QUEUE_WAIT_TIMEOUT),
        so this set is now populated only by explicit halt_all_instances() calls. These
        agents should wait cooperatively and resume automatically when
        resume_all_instances() clears the flag.
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
            self._release_slot(instance, inst_name, 'compression_halt', action='drop-handoff')

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
                    ok = self.reacquire_for(instance, inst_name, context='after_compression_resume')
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
        return (self._is_terminal_stop(inst_name) or inst_name in self.pool._halted_instances)

    def _check_stream_termination(self, stream_tick: int, inst_name: str, response: List[Message],
                                  turn_output: List[Message],
                                  partial_msgs: List[Message]) -> Optional[Tuple[List[Message], bool]]:
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
                logger.debug('[TERMINATE] Stopped mid-stream after %d ticks - %s', stream_tick, inst_name)
                return (response + turn_output + partial_msgs, False)
        return None

    def _inject_async_messages(self, instance: AgentInstance, messages: List[Message], llm_messages: List[Message],
                               response: List[Message]) -> bool:
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
                instance,
                inst_name,
                messages,
                llm_messages,
                response,
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
                # all changes. extra.function_id is compared too so a tool-call batch whose
                # identity changed (e.g. an id assigned after the first delta, or a merge/
                # re-split of parallel calls) is always detected even when name+args are equal.
                if (getattr(old_msg, 'content', None) != getattr(new_msg, 'content', None) or
                        getattr(old_msg, 'reasoning_content', None) != getattr(new_msg, 'reasoning_content', None) or
                        getattr(old_msg, 'function_call', None) != getattr(new_msg, 'function_call', None) or
                        (getattr(old_msg, 'extra', None) or {}).get('function_id') !=
                        (getattr(new_msg, 'extra', None) or {}).get('function_id')):
                    needs_update = True
                    break

        if needs_update:
            instance._streaming_responses = copy.deepcopy(last_output)

    def _make_retrying_message(self,
                               instance: AgentInstance,
                               attempt: int,
                               max_retries: int,
                               delay: float,
                               error=None) -> Message:
        """Create [RETRYING] notification message for UI.

        Extracted from _call_llm_with_injection() - Phase 3.6

        Args:
            instance: Agent instance
            attempt: Current retry attempt number
            max_retries: Maximum retries allowed
            delay: Seconds until next retry
            error: Optional exception that triggered the retry. When provided, the message
                names the actual cause (e.g. "server error (HTTP 502)") via
                classify_endpoint_failure; when None (legacy/other callers) it keeps the exact
                original text "Connection lost" (plan §3.5).

        Returns:
            Transient Message object (not added to conversation history)
        """
        from agent_cascade.error_reporting import classify_endpoint_failure
        reason = classify_endpoint_failure(error) if error is not None else 'Connection lost'
        return Message(role=ASSISTANT,
                       content=f"[RETRYING] {reason}, retrying ({attempt}/{max_retries}) in {delay:.1f}s...")

    def _handle_inner_loop_detection(self, instance: AgentInstance, e: Exception, retry_count: int,
                                     loop_retry_count: int, _max_attempts: int) -> None:
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
                tel.record_loop_detected(inst_name,
                                         reason=_det_reason,
                                         auto_rolled_back=False,
                                         pop_count=0,
                                         loop_type='inner')
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
            logger.warning(f"[INNER_LOOP] Endpoint cursor advanced for '{inst_name}' "
                           f"to position {new_pos} (detection: {_reason}). "
                           f"Next retry will use a different endpoint.")
        else:
            logger.info(f"[INNER_LOOP] Detection triggered for '{inst_name}' "
                        f"(reason: {_reason}), but not strong enough to advance cursor. "
                        f"Retrying same endpoint.")

    def _record_telemetry_event(self, inst_name: str, event_type: str, **kwargs) -> None:
        """Record telemetry event for LLM call lifecycle.

        Lightweight wrapper to reduce inline noise in retry logic.
        Failures are silently swallowed — telemetry must never break LLM calls.

        Also stamps the instance's LLM-activity fields ('start'/'end') that the
        FIFO head-stall alarm reads (plan §3.2 write site A). The per-chunk stamp
        (write site B) lives in the streaming watchdog block, where the timestamp
        is already being taken.

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
                    tel.record_llm_call_end(inst_name,
                                            output_tokens_est=kwargs.get('output_tokens_est', 0),
                                            last_output=last_output)
                elif event_type == 'first_token':
                    tel.record_llm_first_token(inst_name, **kwargs)
        except Exception:
            pass
        # LLM-activity stamp (plan §3.2): covers prefill (start) and completion
        # (end) for every retry attempt — the same boundaries telemetry uses.
        # Best-effort: test doubles may use a pool stub without get_instance, so
        # guard it exactly like the telemetry block above (must never break the call).
        if event_type in ('start', 'end'):
            try:
                inst = self.pool.get_instance(inst_name)
            except Exception:
                # Diagnostic path — must never break LLM calls (same principle as the
                # telemetry block above); test doubles may stub the pool without it.
                inst = None
            if inst is not None:
                self._stamp_llm_activity(inst, event_type)

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

    def _log_messages_to_jsonl(self, instance: AgentInstance, inst_name: str, turn_output: List[Message]) -> None:
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

        already_logged_count = len(log_inst.data.get('history', []))
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
                _check_tail(inst_name, conv, log_inst.log_path, context='log_messages')
        except Exception as e:
            logger.debug(f"Logging message to file failed for {inst_name} (non-critical): {e}")

    def _check_and_handle_truncation(self, is_truncated: bool, turn_output: List[Message], instance: AgentInstance,
                                     inst_name: str, messages: List[Message], llm_messages: List[Message],
                                     response: List[Message]) -> bool:
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
        # BUG_0048: a text-bearing, tool-call-free turn is structurally ambiguous — the
        # structural detector cannot tell "final answer" from "announced more work".
        # Ask the decision model; an 'incomplete' verdict becomes an opaque tag that lands
        # in the EXISTING generic retry path below (rollback + rebuild + clear). Gate on BOTH
        # toggles before the call so either being off costs zero HTTP and is byte-identical
        # to the pre-fix path. getattr default True: the feature is on by default.
        if (is_incomplete is None and self.pool.settings.auto_continue
                and getattr(self.pool.settings, 'completion_classifier_enabled', True)):
            verdict = classify_completion(self.pool, _last_assistant_text(turn_output))
            if verdict == 'incomplete':
                is_incomplete = 'text-only-incomplete'
        if (is_truncated or
                is_incomplete) and not self._is_terminal_stop(inst_name) and self.pool.settings.auto_continue:
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
            if is_incomplete == 'reasoning-only' and instance._reasoning_only_soft_attempts < REASONING_ONLY_CONTINUE_ATTEMPTS:
                instance._reasoning_only_soft_attempts += 1  # budget: how many soft tries this episode
                reason = f"incomplete state (reasoning-only, soft continue {instance._reasoning_only_soft_attempts}/{REASONING_ONLY_CONTINUE_ATTEMPTS})"
                if (tel := self._telemetry()) is not None:
                    try:
                        tel.record_auto_continue(inst_name, reason=reason)
                    except Exception:
                        pass
                logger.info(
                    f"Detected incomplete state (reasoning-only) for {inst_name}. Soft continue attempt {instance._reasoning_only_soft_attempts}/{REASONING_ONLY_CONTINUE_ATTEMPTS}."
                )
                # Default: pure resend — re-call on the SAME history (reasoning msg stays in place); nothing is popped or appended.
                if SOFT_CONTINUE_NUDGE_ENABLED:
                    # (Deferred feature — OFF by default.) Inject an escalating USER nudge so the
                    # model gets an explicit instruction instead of a bare resend. When enabled, a
                    # nudge is now in context, so track it for rollback accounting on the later full retry.
                    instance._reasoning_only_pending_nudges += 1
                    self._inject_soft_continue_nudge(instance, inst_name, messages, llm_messages, response)
                return True

            # existing full-retry path (truncation / broken-json / empty-output / reasoning-only after N soft tries)
            reason = 'truncation' if is_truncated else f"incomplete state ({is_incomplete})"
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
            if is_incomplete == 'reasoning-only':
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
            return ('You have produced reasoning-only output again with no visible result. '
                    'STOP thinking and MUST produce either a final answer or a concrete tool call '
                    'on this turn — do not emit another reasoning-only message.')
        return ('Your last turn contained only reasoning/thinking and no output or tool call. '
                'Please continue — produce your response text or make the next tool call now.')

    def _tail_sync_conversation(self, instance: AgentInstance, inst_name: str, context: str) -> None:
        """Run the best-effort tail-sync JSONL check after a conversation append.

        Callers own activity marking and exception handling; this helper only performs
        the optional tail-sync check so the shape lives in one place.
        """
        if not getattr(self.pool.settings, 'tail_sync_check_enabled', True):
            return
        from agent_cascade.logger.tail_sync_check import check_and_log as _check_tail
        with instance._compression_lock:
            conv = instance.conversation
        log_inst = self.pool.get_logger(inst_name, instance.agent_class)
        _check_tail(inst_name, conv, log_inst.log_path, context=context)

    def _sync_conversation_log(self, instance: AgentInstance, inst_name: str, context: str) -> None:
        """Mark activity + run the tail-sync JSONL check after a conversation append (house pattern).

        Keeps the JSONL log in sync with ``instance.conversation`` on any path that appends to it.
        Best-effort: failures are logged at debug level and never propagate.
        """
        try:
            self.pool._mark_activity(inst_name)
            self._tail_sync_conversation(instance, inst_name, context)
        except Exception as e:
            logger.debug(f"Conversation log sync failed for {inst_name} (non-critical): {e}")

    def _inject_soft_continue_nudge(self, instance: AgentInstance, inst_name: str, messages: List[Message],
                                    llm_messages: List[Message], response: List[Message]) -> None:
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
            messages.append(msg)  # full working set
            response.append(msg)  # local accumulator (UI visibility)
            self._append_and_log_to_llm(instance, msg, llm_messages, lock_held=True)  # conversation + JSONL log + LLM view (exactly once)
        # Keep the JSONL log in sync with the appended nudge (house pattern).
        self._sync_conversation_log(instance, inst_name, 'reasoning_soft_continue')

    def _process_response(self, instance: AgentInstance, turn_output: List[Message], messages: List[Message],
                          llm_messages: List[Message], response: List[Message]) -> bool:
        """Phase 4: Normalize response, handle auto-continue on truncation, execute tools.

        Returns True if processing should continue to next iteration (tool was used or truncated).
        """
        inst_name = instance.instance_name

        # FIX
        is_truncated = any(_check_message_truncation(msg) for msg in turn_output)

        # Extracted to _normalize_turn_output() - Phase 3.3 (FIX
        self._normalize_turn_output(turn_output)

        # Commit + clear ATOMIC under _compression_lock so no concurrent serialization can
        # observe "committed but not cleared" (the commit-race window that let a stale
        # in-flight partial be appended as a near-duplicate alongside the committed final).
        # _append_and_log_batch(lock_held=True) reuses this lock (RLock, reentrant) and logs
        # each msg exactly once — identical logging to the previous non-locked call.
        with instance._compression_lock:
            self._append_and_log_batch(instance, turn_output, lock_held=True)
            response.extend(turn_output)  # Separate list for streaming/accumulation
            # Streaming UI Content Update Fix: Clear _streaming_responses in the SAME locked
            # section as the commit above so the two are atomic w.r.t. serialization.
            instance._streaming_responses = []

        # TG-STREAM F1: push the run's FIRST text output at commit time —
        # the turn is now durably committed, so its assistant text is safe to deliver.
        # Best-effort (never raises); no-op for sub-agents / tool-call-only turns.
        _tg_stream_push(instance, turn_output, self.pool)

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

                        logger.debug(
                            f"[CONTINUE_FIX] Merged continue-saved assistant message ({len(old_content)} chars) with new response ({len(new_content)} chars)"
                        )
                        merged = True
                        break

                if not merged:
                    # Fallback: saved message was popped from conversation by
                    # continue handler.
                    # If we can't merge it, re-append it to prevent data loss.
                    logger.warning(f"[CONTINUE_FIX] Could not merge continue-saved message for {inst_name}: "
                                   f"no assistant message found in turn_output. Re-appending as separate message.")
                    self._append_and_log(instance, saved)
                    instance._continue_fallback_append = True

        # Extracted to _check_and_handle_truncation() - Phase 3.3
        if self._check_and_handle_truncation(is_truncated, turn_output, instance, inst_name, messages, llm_messages,
                                             response):
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
                instance,
                inst_name,
                messages,
                llm_messages,
                response,
        ):
            return True  # Continue to next LLM call for urgent message processing

        # FIX
        # the turn is complete (no continue message injected), so return False
        # to avoid infinite loop.
        return used_any_tool

    def _check_for_tool_calls_in_output(self, instance: AgentInstance, response: List[Message]) -> bool:
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

    def _detect_pure_thinking_turn(self, instance: AgentInstance, response: List[Message]) -> bool:
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
            if (m.get('role') == ASSISTANT or getattr(m, 'role', '') == ASSISTANT))

        has_thinking = any(m.get('thought') or m.get('reasoning_content') for m in response[-3:])

        # Pure thinking turn — continue to next turn
        if not has_real_content and has_thinking:
            logger.info(f"Pure reasoning turn detected for {inst_name}. Continuing.")
            return True

        return False

    def _is_genuine_completion(self, instance: AgentInstance, response: List[Message]) -> bool:
        """True only when a Phase-5 break is a GENUINE natural completion (a real final
        answer), as opposed to an excluded exit.

        ``_post_turn_checks`` returns False via four distinct paths that all collapse into
        the same ``if not completed:`` in run(): terminal stop, terminal stop during the
        compression wait, pure-thinking stall, and genuine completion. The auto-skill
        reflection must fire only on the last one — a stopped or stalled agent has not
        delivered a real answer to reflect on. This helper mirrors exactly the two
        predicates ``_post_turn_checks`` uses for its non-completion break paths
        (``_is_terminal_stop`` and ``_detect_pure_thinking_turn``) so the gating cannot
        drift from the method it guards.

        Args:
            instance: The agent being executed.
            response: Response list from the just-finished turn.

        Returns:
            True only when NOT terminal-stop AND NOT a pure-thinking stall.
        """
        if self._is_terminal_stop(instance.instance_name):
            return False  # terminal stop (or stop during compression wait) — excluded
        if self._detect_pure_thinking_turn(instance, response):
            return False  # pure reasoning turn — agent stalled, not completed
        return True

    def _transition_to_sleeping_if_pending(self, instance: AgentInstance, inst_name: str) -> bool:
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

    def _drain_post_generation_messages(self, instance: AgentInstance, inst_name: str, messages: List[Message],
                                        llm_messages: List[Message], response: List[Message]) -> bool:
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
                    instance,
                    inst_name,
                    messages,
                    llm_messages,
                    response,
            ):
                return True  # Continue loop to process drained results
        except Exception as e:
            logger.error('Safety drain failed for %s: %s', inst_name, e)

        return False

    def _post_turn_checks(self, instance: AgentInstance, messages: List[Message], llm_messages: List[Message],
                          response: List[Message]) -> bool:
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
            logger.debug('post-turn processing suspended by compression - %s', inst_name)
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
        if self._drain_post_generation_messages(instance, inst_name, messages, llm_messages, response):
            return True  # Messages drained — continue looping

        return False  # Agent has truly completed





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
                    # Save KV state ONLY when there is actually pending work to wait on —
                    # i.e. an async agent was dispatched THIS turn and we're sleeping for it.
                    # At this point the parent still holds its slot (released just below), so
                    # saving targets the HELD endpoint (eviction-safe: FIFO guarantees
                    # single-holder for conc=0). This save sets _state_label, which is what
                    # lets the sleep-wakeup restore (_handle_sleeping_state) reload our KV
                    # cache when the async child returns.
                    #
                    # The has_pending() gate is what stops us writing 3.1GB on EVERY turn:
                    # on a natural turn end with no dispatch, pool.has_pending() is False,
                    # so we skip the save, _state_label stays unset, and both _setup_turn
                    # and the sleep-wakeup path skip their restore — the warm KV cache is
                    # preserved (no 85K-token reprocess). We do NOT gate on "dispatched THIS
                    # turn" via a flag because pool.has_pending() is exactly that signal for
                    # the async case (a pending call_agent == an agent we dispatched and are
                    # waiting on).
                    if self.pool.has_pending(instance.instance_name):
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
                    release_slot_permit(instance,
                                        instance.instance_name,
                                        action='drop-sleep',
                                        context='sleep transition',
                                        pool=_sleep_pool)

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
                # NOTE (LEAK #2): if the instance still holds a permit here, it is NOT
                # released at this site — any later re-acquire through
                # _acquire_slot_with_logging will capture-and-release it with a
                # [SLOT_STALE_PERMIT] WARNING. Correlate the two lines when triaging.
                logger.warning(f"_transition_to_sleeping skipped for {instance.instance_name}: "
                               f"current state={instance.state.name} (expected RUNNING)")

    # ═══════════════════════════════════════════════════════════════════════
    #  State Handling — SLEEPING state extraction (Phase 3.1)
    # ═══════════════════════════════════════════════════════════════════════

    def _handle_sleeping_state(self, instance: 'AgentInstance', messages: List[Message], llm_messages: List[Message],
                               response: List[Message]) -> Tuple[SleepAction, Optional[List[Message]]]:
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
                logger.debug('RESUMED from SLEEPING - %s (%d messages)', inst_name, len(messages_list))

            # Inject all drained messages as user-type messages (async results are already formatted strings)
            self._drain_and_inject(
                instance,
                inst_name,
                messages,
                llm_messages,
                response,
                items=messages_list,  # Pass pre-drained items directly
            )

            # Re-acquire concurrency slot after waking from SLEEPING
            self._acquire_slot_with_logging(instance, 'after_message_wakeup')
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
                logger.error(f"SLEEPING timeout for {inst_name}: waited "
                             f"{int(sleeping_duration)}s (max "
                             f"{AGENT_SLEEPING_MAX_WAIT_SECONDS}s). Forcing COMPLETING.")
                with instance._state_lock:
                    if instance.state == AgentState.TERMINATED:
                        return SleepAction.BREAK_LOOP, None
                    instance._transition(AgentState.COMPLETING)
                    instance.sleeping_since = None
                # Enqueue an informational message. The agent is exiting the run()
                # loop (BREAK_LOOP), so this won't be re-processed by its own loop;
                # it remains in the queue as a log artifact / debugging context.
                self.pool.enqueue_message(
                    inst_name, f"[SYSTEM] SLEEPING timeout: pending background tools did not "
                    f"complete within {AGENT_SLEEPING_MAX_WAIT_SECONDS}s "
                    f"(waited {int(sleeping_duration)}s). Forcing completion.")
                return SleepAction.BREAK_LOOP, None

            # Get settings with defaults
            wakeup_interval = getattr(self.pool.settings, 'sleeping_wakeup_interval', 5.0)

            # Log wakeup message periodically
            if (current_time - instance._last_wakeup_log) >= wakeup_interval:
                logger.info('SLEEPING - %s waiting %.1fs for background tools', inst_name, sleeping_duration)
                instance._last_wakeup_log = current_time

            try:
                if (current_time - instance._last_waiting_debug_log) >= 5.0:
                    logger.debug('WAITING for background tools - %s (%.1fs)', inst_name, sleeping_duration)
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
                    instance,
                    inst_name,
                    messages,
                    llm_messages,
                    response,
            ):
                results_found = True

            # Final safety drain — catches race conditions
            if self._drain_and_inject(
                    instance,
                    inst_name,
                    messages,
                    llm_messages,
                    response,
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
                self._acquire_slot_with_logging(instance, 'after_stable_drain')

                # Restore KV cache after slot acquired, before resuming execution —
                # gated on actual slot ownership and targeting the held endpoint only
                # (eviction safety; _slot_release is None for unlimited/no-slot endpoints).
                if instance._slot_release is not None:
                    self._restore_held_slot_state(instance, inst_name)

                # Exit if stopped after re-acquiring slot in sleep loop
                if self._is_terminal_stop(inst_name):
                    logger.debug(f"[SLOT_STOP_CHECK] Terminal stop after stable drain for {inst_name}, exiting")
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
            logger.debug('COMPLETING - %s (no pending tools)', inst_name)
            return SleepAction.BREAK_LOOP, None

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
