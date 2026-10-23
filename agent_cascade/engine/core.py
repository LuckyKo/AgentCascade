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

import time
from typing import Any, Callable, List, Optional, Tuple

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.compression.handler import CompressionHandler
from agent_cascade.exceptions import (CharacterRunDetected, ContextWindowExceeded,
                                      MaxTokenExceeded)
from agent_cascade.lifecycle_manager import AgentLifecycleManager
from agent_cascade.llm.schema import ASSISTANT, USER, Message
from agent_cascade.log import logger
from agent_cascade.settings import (AUTO_SKILL_ENABLED, AUTO_SKILL_EXTRA_TURNS,
                                    AUTO_SKILL_MIN_TURNS, DEFAULT_LOAD_SKILL_MODE, LOAD_SKILL_NONE)
from agent_cascade.stream_publisher import StreamPublisher
from agent_cascade.tool_dispatcher import ToolDispatcher
from agent_cascade.utils.utils import extract_text_from_message, msg_field, msg_set

# ── Constants (core) ───────────────────────────────────────────────────────────
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


            # Clean thinking blocks from function call arguments (P4
            # continuation) — REMOVED: json_loads already strips pre-parse,
            # and per-value normalization is a corruption risk.

















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
