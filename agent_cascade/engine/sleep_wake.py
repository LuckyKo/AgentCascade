"""Sleep/wake handling for the execution engine (Phase 5 split).

``SleepWakeMixin`` owns the SLEEPING-state entry/exit path: deciding whether to
sleep on pending async work, the atomic RUNNING -> SLEEPING transition, and the
wakeup/reacquire loop driven by ``run``. Method bodies are moved verbatim from
:mod:`agent_cascade.engine.core`.
"""

from __future__ import annotations

import time
from typing import List, Optional, Tuple

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.engine.helpers import SleepAction
from agent_cascade.llm.schema import Message
from agent_cascade.log import logger
from agent_cascade.settings import AGENT_SLEEPING_MAX_WAIT_SECONDS


class SleepWakeMixin:
    """Transition into SLEEPING and handle wakeups from pending async work."""

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
