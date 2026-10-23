"""Run-loop orchestration for the execution engine (Phase 5 split).

``RunLoopMixin`` owns the main generator entry point and its phase helpers:
preflight/slot acquisition, turn setup, budget warnings, the LLM streaming
phase, response processing, post-turn checks, and final cleanup. Method bodies
are moved verbatim from :mod:`agent_cascade.engine.core`; the only call-site
change is reading the auto-skill extra-turn budget through the shared
``_get_auto_skill_extra_turns`` accessor.
"""

from __future__ import annotations

import time
from enum import Enum, auto
from typing import Iterator, List, Optional, Tuple, Union

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.engine.helpers import SleepAction
from agent_cascade.engine.telegram_stream import _tg_push_final
from agent_cascade.exceptions import (AgentTerminatedError, CharacterRunDetected, ContextWindowExceeded,
                                      MaxTokenExceeded)
from agent_cascade.llm.schema import ASSISTANT, Message
from agent_cascade.log import logger
from agent_cascade.settings import (DEFAULT_MAX_TURNS, SOFT_TURN_LIMIT, SOFT_TURN_LIMIT_ABSOLUTE_MAX,
                                    SOFT_TURN_LIMIT_WARNING_INTERVAL)
from agent_cascade.tool_utils import clear_truncation_state
from agent_cascade.utils.utils import msg_field

SLEEPING_LOOP_BACKOFF = 0.1  # Seconds to sleep when re-entering loop from SLEEPING state
_COMPRESSION_WAIT_TIMEOUT = 1.0  # Seconds to wait per iteration when suspended by compression


class _RunAction(Enum):
    """Control-flow result returned by the extracted run-loop phase helpers."""
    PROCEED = auto()
    CONTINUE_LOOP = auto()
    BREAK_LOOP = auto()


class RunLoopMixin:
    """Main turn-loop orchestration and phase dispatch."""

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
        self._run_preflight(instance)
        # Initialize variables before try block to handle exceptions during
        # _setup_turn
        messages = None
        llm_messages = None
        response = None
        early_exit = False  # Set True on empty-conversation early exit so the finally block does not double-drain the queue.
        try:
            if not self._run_acquire_initial_slot(instance):
                return

            messages, llm_messages, response = self._run_turn_setup(instance)
            if not messages:
                early_exit = True
                return

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

                final_turn_tools_disabled, _soft_limit_last_warn_past = self._run_budget_warnings(
                    instance, llm_messages, turns_available, max_turns, initial_turns_available,
                    soft_turn_limit, turns_50pct, turns_90pct, _suppress_budget_warnings,
                    _soft_limit_last_warn_past)

                # Change 2 gate signal (todo149): True exactly when turns_available was 1 at the
                # top of this iteration (i.e. this is the last turn of the ORIGINAL budget).
                # Computed BEFORE _consume_turn so it reflects the pre-decrement value. It can
                # never be true mid-run (mid-run turns have turns_available > 1 here). After an
                # extension resets turns_available to AUTO_SKILL_EXTRA_TURNS, a later `== 1` is
                # the REFLECTION's last turn — but by then _auto_skill_proposed is set, so
                # _auto_skill_gates_met returns False and it cannot re-fire (one-shot).
                _is_last_original_turn = (turns_available == 1)
                turns_available = self._consume_turn(instance, turns_available)

                action, turn_output = yield from self._run_llm_phase(instance, llm_messages, response)
                if action == _RunAction.BREAK_LOOP:
                    break
                if action == _RunAction.CONTINUE_LOOP:
                    continue

                action, max_turns, turns_available, _suppress_budget_warnings = \
                    yield from self._run_response_phase(
                        instance, messages, llm_messages, response, turn_output,
                        final_turn_tools_disabled, _is_last_original_turn, max_turns,
                        turns_available, _suppress_budget_warnings)
                if action == _RunAction.CONTINUE_LOOP:
                    continue
                action, max_turns, turns_available, _suppress_budget_warnings = \
                    yield from self._run_post_turn(
                        instance, messages, llm_messages, response, max_turns,
                        turns_available, _suppress_budget_warnings)
                if action == _RunAction.CONTINUE_LOOP:
                    continue
                if action == _RunAction.BREAK_LOOP:
                    break

            self._run_turn_limit_notice(
                instance, response, turns_available, max_turns,
                initial_turns_available, soft_turn_limit, soft_limit_absolute_cap)

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
            self._run_finalize(instance, early_exit)

    def _run_response_phase(self, instance: AgentInstance, messages: List[Message], llm_messages: List[Message],
                            response: List[Message], turn_output: List[Message], final_turn_tools_disabled: bool,
                            is_last_original_turn: bool, max_turns: int, turns_available: int,
                            suppress_budget_warnings: bool) -> Iterator[Tuple[_RunAction, int, int, bool]]:
        """Process this turn's response and tool-call continuation.

        Returns ``(action, max_turns, turns_available, suppress_budget_warnings)``.
        """
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
            if is_last_original_turn and \
                    self._try_auto_skill_extension(
                        instance, messages, llm_messages,
                        loaded_skill_names=getattr(instance, '_loaded_skill_names', None)):
                # Change 3 marker (todo149): the run did not end on a clean no-tool answer
                # before reflecting → extract_instance_output must return the LAST reflection
                # message, not the pre-reflection snapshot.
                instance._auto_skill_dirty_stop = True
                max_turns = self._grant_auto_skill_extension(instance)
                suppress_budget_warnings = True
                turns_available = self._get_auto_skill_extra_turns()
                yield response
                return (_RunAction.CONTINUE_LOOP, max_turns, turns_available,
                        suppress_budget_warnings)
            # logger.debug("tool used - %s looping",
            # instance.instance_name)
            yield response
            return (_RunAction.CONTINUE_LOOP, max_turns, turns_available,
                    suppress_budget_warnings)
        return _RunAction.PROCEED, max_turns, turns_available, suppress_budget_warnings

    def _run_post_turn(self, instance: AgentInstance, messages: List[Message], llm_messages: List[Message],
                       response: List[Message], max_turns: int, turns_available: int,
                       suppress_budget_warnings: bool) -> Iterator[Tuple[_RunAction, int, int, bool]]:
        """Run post-turn completion checks and the natural-completion reflection trigger.

        Returns ``(action, max_turns, turns_available, suppress_budget_warnings)``.
        """
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
                    suppress_budget_warnings = True
                    # NOTE: this trigger fires at Phase 5 (natural completion), i.e. AFTER
                    # _consume_turn has already decremented turns_available for the triggering
                    # turn — so the reset is exactly AUTO_SKILL_EXTRA_TURNS (NOT +1). The old
                    # budget-exhaustion design fired BEFORE _consume_turn, which is why its
                    # reset carried a +1; that no longer applies at this call site.
                    turns_available = self._get_auto_skill_extra_turns()
                    yield response
                    return (_RunAction.CONTINUE_LOOP, max_turns, turns_available,
                            suppress_budget_warnings)  # run the reflection turns
            return (_RunAction.BREAK_LOOP, max_turns, turns_available,
                    suppress_budget_warnings)
        return _RunAction.PROCEED, max_turns, turns_available, suppress_budget_warnings

    def _run_budget_warnings(self, instance: AgentInstance, llm_messages: List[Message], turns_available: int,
                             max_turns: int, initial_turns_available: int, soft_turn_limit: bool,
                             turns_50pct: int, turns_90pct: int, suppress_budget_warnings: bool,
                             soft_limit_last_warn_past: int) -> Tuple[bool, int]:
        """Emit 50%/90%/final/soft-overage warnings for the current turn.

        Returns ``(final_turn_tools_disabled, soft_limit_last_warn_past)``.
        """
        final_turn_tools_disabled = False
        # Turn limit warnings (50%, 90%, final) — one-time only, emitted as SEPARATE USER messages.
        # Checked BEFORE decrement so max_turns=1 agents still get the final warning.
        # Each threshold fires at most once (exact integer equality + single-step decrement).
        # Pattern: _make_user_message → _append_and_log_to_llm, which appends to
        # conversation + JSONL log and mirrors into llm_messages exactly once.
        # NOTE: these are NOT added to the local `messages` list because _setup_turn()
        # runs ONCE per run (not per iteration); loop detection/compression thus see a view
        # that omits them — same as the final-turn warning. Bounded (<=2 messages) and does
        # not affect LLM context, which uses llm_messages.
        if not suppress_budget_warnings and turns_available == turns_50pct:
            warn_msg = (f"[SYSTEM WARNING: Halfway through your turn budget. "
                        f"You have {turns_available} turn(s) remaining out of {max_turns} total. "
                        f"Assess your progress and plan remaining steps.]")
            warn_user = self._make_user_message(warn_msg)
            self._append_and_log_to_llm(instance, warn_user, llm_messages)
        if not suppress_budget_warnings and turns_available == turns_90pct:
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
            if turns_past_limit - soft_limit_last_warn_past >= SOFT_TURN_LIMIT_WARNING_INTERVAL:
                over_msg = self._make_user_message(
                    f"[SYSTEM WARNING: You are over the turn limit, please wrap up. "
                    f"{turns_past_limit} turn(s) past the {max_turns}-turn limit.]")
                self._append_and_log_to_llm(instance, over_msg, llm_messages)
                soft_limit_last_warn_past = turns_past_limit
        elif turns_available > 0:
            # A fresh budget (including an auto-skill reflection reset) gets its
            # own 5-turn overage cadence.
            soft_limit_last_warn_past = 0
        return final_turn_tools_disabled, soft_limit_last_warn_past

    def _run_llm_phase(self, instance: AgentInstance, llm_messages: List[Message],
                       response: List[Message]) -> Iterator[Tuple[_RunAction, List[Message]]]:
        """Drive the LLM call and streaming loop for one turn.

        Yields the same streaming/termination tuples as before and returns
        ``(action, turn_output)`` so the caller can apply main-loop control.
        """
        inst_name = instance.instance_name
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
                return _RunAction.BREAK_LOOP, turn_output
            elif self._is_suspended_by_compression(instance.instance_name):
                # Compression-halt is a suspension, not termination — wait cooperatively.
                logger.debug('suspended by compression, waiting - %s', instance.instance_name)
                if not self._wait_for_compression_to_clear(instance.instance_name):
                    yield response
                    return _RunAction.BREAK_LOOP, turn_output  # Terminal stop during wait
                yield response
                return _RunAction.CONTINUE_LOOP, turn_output  # Resumed (compression done)
        return _RunAction.PROCEED, turn_output

    def _run_preflight(self, instance: AgentInstance) -> None:
        """Prepare run state and generation before entering the guarded loop."""
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

    def _run_acquire_initial_slot(self, instance: AgentInstance) -> bool:
        """Check for a terminal stop and acquire the initial concurrency slot.

        Returns False when the run must exit immediately; the caller's finally
        still performs the normal IDLE/state cleanup.
        """
        if self._is_terminal_stop(instance.instance_name):
            return False  # Terminal stop — don't start work (exit finally → IDLE)

        self._acquire_slot_with_logging(instance, 'initial')

        # Exit if stopped after slot acquire — prevents stale slot reuse post-stop
        if self._is_terminal_stop(instance.instance_name):
            self._release_slot(instance, instance.instance_name, action='drop-exit')
            return False  # Terminal stop — release slot and exit (exit finally → IDLE)
        return True

    def _run_turn_setup(self, instance: AgentInstance):
        """Run ``_setup_turn`` plus the empty-conversation drain/re-entry safety net.

        Returns ``(messages, llm_messages, response)`` or ``(None, None, None)``
        for the documented early-exit path.
        """
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
                return None, None, None
        return messages, llm_messages, response

    def _run_turn_limit_notice(self, instance: AgentInstance, response: List[Message], turns_available: int,
                               max_turns: int, initial_turns_available: int, soft_turn_limit: bool,
                               soft_limit_absolute_cap: int) -> None:
        """Append the turn-limit notice after a normal loop exit."""
        inst_name = instance.instance_name
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

    def _run_finalize(self, instance: AgentInstance, early_exit: bool) -> None:
        """Run the ``run()`` finally cleanup: telemetry, queue/slot, sync, and state."""
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
