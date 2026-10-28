"""Slot lifecycle helpers for the execution engine (Phase 2 split).

``SlotLifecycleMixin`` owns slot acquisition/release/reacquire, stale-permit
handling, KV save/restore around yields, and LLM-activity stamping. Method
bodies are moved VERBATIM from :mod:`agent_cascade.engine.core`; the mixin is
composed into ``ExecutionEngine`` there so class-method lookups and instance
patch points remain unchanged.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.exceptions import AgentTerminatedError
from agent_cascade.log import logger
from agent_cascade.settings import POST_YIELD_REACQUIRE_TIMEOUT, REACQUIRE_TIMEOUT


class SlotLifecycleMixin:
    """Acquire, release, reacquire, and persist state around concurrency slots."""

    @staticmethod
    def _safe_note_held_endpoint(router, agent_class: str, instance_name: str) -> None:
        """Best-effort last-released bookkeeping: record the endpoint an acquire resolved to.

        Centralizes the guard+call pattern shared by every slot-acquire funnel (initial,
        sleep-wakeup, and both reacquire_for grants). ``note_held_endpoint`` itself never
        raises, but the call is wrapped here too so a router without the method (older/patched
        builds) or any unexpected failure can never break slot acquisition. Pure bookkeeping —
        distinct from the committed probe-gate marker.
        """
        try:
            if router is not None and hasattr(router, 'note_held_endpoint'):
                router.note_held_endpoint(agent_class, instance_name)
        except Exception:
            pass

    def _acquire_slot_with_logging(self, instance: AgentInstance, context: str = 'initial') -> None:
        """Acquire concurrency slot with debug logging.

        Args:
            instance: The agent instance acquiring the slot
            context: Description of acquisition context ("initial", "after_async_wakeup", etc.)
        """
        if not hasattr(self.pool, '_acquire_slot'):
            return

        try:
            # LEAK #2: this helper is re-entered by the sleep-wakeup paths
            # (after_message_wakeup / after_stable_drain) which can run while the
            # instance still holds a permit. Capture-and-release any prior live
            # permit BEFORE acquiring; a plain assignment here silently orphans
            # the old SlotHolder in pool._running (keyed by instance_name) and
            # logs nothing.
            self._discard_stale_permit(instance, instance.instance_name,
                                       context=f'slot re-acquire ({context})', action='drop-stale-reacquire')
            instance._slot_release = self.pool._acquire_slot(instance.agent_class, instance.instance_name)
            # Store slot key for diagnostics. Cursor-aware resolution (sticky slot
            # plan change #6): the key must match the endpoint pool that
            # _acquire_slot actually acquired (chain rotated by this instance's
            # cursor), not the raw chain head.
            if instance._slot_release is not None:
                router = self.pool.api_router
                if router:
                    if hasattr(router, 'get_effective_slot_info'):
                        slot_info = router.get_effective_slot_info(instance.agent_class,
                                                                   instance_name=instance.instance_name)
                    else:
                        slot_info = router.get_agent_slot_info(instance.agent_class)
                    instance._slot_key = slot_info.get('slot_key')
                    # Best-effort last-released bookkeeping: record the endpoint this acquire
                    # resolved to so release_slot_permit can update _last_released_endpoint even
                    # if the LLM call never completes (yield/reacquire cycle). Distinct from the
                    # committed probe-gate marker; never raises.
                    self._safe_note_held_endpoint(router, instance.agent_class, instance.instance_name)
            logger.debug(f"[SLOT] {instance.instance_name} ({instance.agent_class}): acquired slot [{context}]")
        except AgentTerminatedError:
            # Clean abort — don't log as error, just propagate for caller to handle
            raise
        except Exception as e:
            logger.error(f"[SLOT_ACQUIRE_FAILED] {context} for {instance.instance_name}: {e}")
            raise

    def _resolve_held_endpoint(self, instance: AgentInstance) -> Optional[dict]:
        """Resolve the endpoint the instance currently holds a slot on.

        Thin delegate to ``state_ops.resolve_held_endpoint`` (the body moved there
        verbatim per the held-endpoint extraction); kept as a method so existing
        engine call sites and tests are untouched.
        """
        from agent_cascade.state_ops import resolve_held_endpoint
        return resolve_held_endpoint(self.pool, instance)

    def _restore_held_slot_state(self, instance: AgentInstance, inst_name: str, label: Optional[str] = None) -> bool:
        """Restore KV state to the endpoint the instance currently holds a slot on.

        Thin delegate to ``state_ops.restore_held_slot_state`` (body moved there
        verbatim); kept as a method so existing engine call sites and tests are
        untouched. ``label`` defaults to None (consume-once ``_state_label`` behavior);
        pass the stable instance-name label to make the restore independent of any
        system agent that may have consumed ``_state_label`` inside the window.
        """
        from agent_cascade.state_ops import restore_held_slot_state
        return restore_held_slot_state(self.pool, instance, inst_name, label=label)

    @staticmethod
    def _clear_orphaned_state_label(instance: AgentInstance, inst_name: str) -> None:
        """Clear a pending _state_label that could not be consumed by a restore.

        Thin delegate to ``state_ops.clear_orphaned_state_label`` (body moved there
        verbatim); kept as a @staticmethod so existing engine call sites and tests
        (which bind this unbound form) are untouched. Best-effort: never raises.
        """
        from agent_cascade.state_ops import clear_orphaned_state_label
        return clear_orphaned_state_label(instance, inst_name)

    def save_before_slot_yield(self, instance: AgentInstance, inst_name: str, reason: str = '') -> bool:
        """Persist KV state just before the caller yields its slot to a system agent.

        Called by every ``yield_caller_slot`` production caller (Security / Compressor)
        via an injected callback so the save happens *before* the permit is released —
        at that instant the caller still owns the endpoint, and FIFO guarantees it is the
        sole holder for conc=0, so the save lands on its own held endpoint (eviction-safe).
        This makes the KV durable across the yield window: if an interleaved Compressor
        model swap evicts the resident cache while the caller is slotless, the persisted
        label survives and can be restored on re-acquisition.

        Delegates to ``state_ops.save_instance_state`` (which self-gates: no-op for
        non-autoloader / state-save-disabled / missing-endpoint instances) under the
        instance's ``_state_lock``, matching the lock discipline of the compression-halt
        save above. Never raises — a missed save degrades to today's behavior, never worse.

        Returns:
            True if a label was saved (a restore is pending), False otherwise.
        """
        from agent_cascade.state_ops import kv_save_effectively_enabled
        # KV gate: global checkpoint toggle ORed with the safe-mode override (see state_ops.kv_save_effectively_enabled).
        # Default-off: the engine's pressure planner handles state retention (demote-to-host). Users may still
        # opt in explicitly via the pool's state_kv_save_enabled setting; safe-mode forces it on for its window.
        if not kv_save_effectively_enabled(instance, self._get_pool_setting('state_kv_save_enabled', False)):
            logger.debug('[STATE_SAVE_SKIP] %s (%s) — KV save before slot yield disabled by toggle',
                         inst_name, reason or 'slot-yield')
            return False
        try:
            from agent_cascade.state_ops import save_instance_state
            with instance._state_lock:
                return bool(save_instance_state(instance))
        except Exception as e:
            logger.debug('[STATE_SAVE_SKIP] %s (%s) — save before slot yield failed: %s',
                         inst_name, reason or 'slot-yield', e)
            return False

    def reacquire_after_slot_yield(self, instance: AgentInstance, inst_name: str, context: str = '',
                                   *, tolerate_failure: bool = False, respect_toggle: bool = True,
                                   skip_kv_restore: bool = False, defer_kv_restore: bool = False) -> bool:
        """Re-acquire the caller's slot after a yield and (normally) restore its KV.

        Shared re-acquire + restore for every ``yield_caller_slot`` production caller
        (Security / Compressor / sync child / parallel call_agent batch). Wraps
        :meth:`reacquire_for` (bounded FIFO,
        no-slot handling) and then restores the saved KV — but ONLY when both safety gates
        hold:

          1. The instance actually re-acquired a slot (``_slot_release is not None``) —
             restoring onto an endpoint we don't hold would auto-evict a live sibling.
          2. A saved state label is pending (``_state_label is not None``) — the consume-once
             gate that stops a redundant multi-GB reload when nothing evicted in the window.

        The restore is routed through :meth:`_restore_held_slot_state` (never a bare
        ``restore_instance_state(instance)``), so it targets the endpoint the instance
        ACTUALLY holds, not the stale ``_last_endpoint_config`` — this also fixes the sync-child
        path that previously restored ungated against the stale config.

        The orphaned label is cleared on every skip path (re-acquire returned False, no slot
        held, resolution failure, or an exception) so a leaked label can never re-fire a
        multi-GB restore on later turns — EXCEPT a deferring caller (``defer_kv_restore=True``),
        which intentionally keeps the label pending for the last child / next-turn self-heal
        (see the ``defer_kv_restore`` arg below).

        Args:
            instance / inst_name: The caller that yielded its slot.
            context: Log label (e.g. "after_security_check", "sync child").
            tolerate_failure: When False (default) any re-acquire failure PROPAGATES —
                preserving Security's sanctioned raise-on-hard-failure behavior. When True,
                the failure is swallowed (label cleared, False returned) for callers that are
                mid-turn and must continue (compression).
            respect_toggle: When True (default) the KV restore honors the ``state_kv_save_enabled``
                UI toggle — OFF skips the restore. System-agent yield callers (Security / Compressor)
                keep this default. The sync-child NORMAL-delegation path passes False so its warm-cache
                restore ALWAYS runs regardless of the toggle (the save side is already ungated there, so
                gating only the restore would drop a saved-but-never-restored state and force reprocess).
            skip_kv_restore: When True, re-acquire the slot but DO NOT restore the KV. Used ONLY by the
                shell_cmd ``safe`` approval window: the tool unloads the model immediately before the
                command and restores by the stable label afterwards, so a restore here would land INSIDE
                the command window and race that unload (the autoloader would cold-reload the model
                mid-command). The pending label is still cleared, per the invariant that every skip
                path must clear it. Default False — every existing caller is unchanged.
            defer_kv_restore: When True, re-acquire the slot but DEFER the KV restore WITHOUT clearing
                the pending ``_state_label``. Used by the parallel-``call_agent`` batch optimization: a
                parent that issues several SYNC ``call_agent`` calls in one turn has an identical context
                across them, so only the LAST child needs to swap the parent model back in. Non-last children
                re-acquire their slot (keeping the FIFO permit accounting and the next child's
                ``_slot_release`` gate intact) but leave the label PENDING so the last child's full restore
                still finds it — and so a mid-batch early exit self-heals at the next turn's ``_setup_turn``.
                This is the DEFER counterpart to ``skip_kv_restore`` (which CLEARS the label because its owner
                re-arms the restore by a stable label); the two are opposite marker policies and MUST NOT be
                conflated. The defer short-circuit returns BEFORE the no-slot / skip_kv_restore / toggle /
                restore branches, so the label is never cleared on any defer path — which is what makes the
                deferral safe under early batch exits. Default False — every existing caller is unchanged.

        Returns:
            True if the slot was re-acquired (restored or not), else False.
        """
        try:
            ok = self.reacquire_for(instance, inst_name, context=context)
        except Exception as e:
            # Clear any pending label so it does not leak and re-fire a restore later,
            # then honor the caller's failure policy. A DEFERRING caller (parallel call_agent non-last)
            # must KEEP its label pending for the last child / next-turn self-heal, so it is not cleared here.
            if not defer_kv_restore:
                self._clear_orphaned_state_label(instance, inst_name)
            if tolerate_failure:
                logger.warning(
                    f"[SLOT_REACQUIRE_DEGRADED] {context or 'slot-yield'} for '{inst_name}' "
                    f"failed to re-acquire its slot; continuing WITHOUT a re-acquired slot: {e}",
                    exc_info=True,
                )
                return False
            raise

        if not ok:
            # reacquire_for returned False (no router available) — no slot held.
            if not defer_kv_restore:
                self._clear_orphaned_state_label(instance, inst_name)
            return False

        # DMS post-return re-stamp (t293-followup): the caller was silent through a long sync child /
        # system-agent yield, so last_activity is stale on return — re-stamp. A genuinely wedged caller
        # never reaches here (reacquire_for would not return True), so this cannot mask a real wedge.
        self.pool._mark_activity(inst_name)

        if defer_kv_restore:
            # Parallel-call_agent deferral: the slot is re-acquired (FIFO permit accounting and the next
            # child's _slot_release gate stay intact), but the KV restore is deferred to the LAST child of
            # the batch. The _state_label MUST stay pending — the last child's full restore consumes it, and
            # if the batch exits early the next-turn _setup_turn restore still finds it. Short-circuiting here
            # deliberately bypasses the toggle / no-slot / skip_kv_restore / restore branches so the label is
            # preserved on EVERY path (the opposite of skip_kv_restore, which clears it). See
            # [[defer-per-item-op-to-last-of-static-batch]].
            logger.debug('[STATE_RESTORE_DEFER] %s — deferring KV restore to the last call_agent in the batch; '
                         'keeping _state_label pending (%s)', inst_name, context or 'slot-yield')
            return True

        # The sync-child NORMAL-delegation path passes respect_toggle=False so its warm-cache
        # restore always runs (its save side is already ungated). System-agent yields keep the
        # default True and honor the KV gate: global checkpoint toggle ORed with the safe-mode
        # override (see state_ops.kv_save_effectively_enabled).
        from agent_cascade.state_ops import kv_save_effectively_enabled
        if respect_toggle and not kv_save_effectively_enabled(
                instance, self._get_pool_setting('state_kv_save_enabled', False)):
            logger.debug('[STATE_RESTORE_SKIP] %s (%s) — KV restore after slot yield disabled by toggle',
                         inst_name, context or 'slot-yield')
            # Clear any stale label (save may have occurred before toggle was turned OFF).
            self._clear_orphaned_state_label(instance, inst_name)
            return True

        # Re-acquired. Restore only when we actually hold a slot AND a save is pending.
        with instance._state_lock:
            has_saved_state = instance._state_label is not None
        if instance._slot_release is None:
            # No-slot / unlimited endpoint — nothing to restore onto; clear any label so it
            # does not leak and re-trigger an expensive state/load on a later turn.
            self._clear_orphaned_state_label(instance, inst_name)
            return True
        if skip_kv_restore:
            # Safe-mode window only: the caller (shell_cmd ``safe``) owns the restore in its
            # finally, AFTER the command, so restoring here would race the tool's unload and
            # cold-reload the model inside the command window. Re-acquiring the slot is still
            # required — only the restore is deferred. The label MUST be cleared: the tool's
            # finally restores by the stable label, so a leaked _state_label would re-fire a
            # redundant multi-GB restore at _setup_turn (see .agent_lessons/save-toggle-skip-path-label-clear.md).
            logger.debug('[STATE_RESTORE_SKIP] %s — advisor KV restore deferred to the safe-mode '
                         "tool's post-command restore (%s)", inst_name, context or 'slot-yield')
            self._clear_orphaned_state_label(instance, inst_name)
            return True
        if has_saved_state:
            self._restore_held_slot_state(instance, inst_name)
        else:
            logger.debug('[STATE_RESTORE_SKIP] %s — no saved state to restore after %s '
                         '(keeping warm KV cache)', inst_name, context or 'slot-yield')
        return True

    @staticmethod
    def _release_slot(slot_holder: Any,
                      holder_name: str,
                      context: str = 'cleanup',
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

    @staticmethod
    def _discard_stale_permit(holder: Any, holder_name: str, context: str, action: str) -> bool:
        """Release any live permit held by `holder`, or do nothing.

        THE INVARIANT: no production path may make a live permit invisible
        without returning it to the pool.

        NO PRE-CHECK. This helper deliberately does NOT test
        `_slot_release is not None` before delegating. Any such test is a
        TOCTOU race: a concurrent acquire between the test and the release
        would have its fresh permit cleared-without-released, i.e. it would
        *manufacture* the very zombie holder this helper exists to prevent.
        `release_slot_permit` already performs the check-and-capture
        atomically under `holder._state_lock` (slot_queue.py) and returns
        False idempotently when nothing is held, so the delegate's return
        value is the authoritative answer to "was a live permit found?" —
        with no separate unsynchronized read anywhere.

        Returns True if a live permit was found and released (a bug upstream),
        False if nothing was held (the overwhelmingly common case).
        """
        from agent_cascade.slot_queue import release_slot_permit
        if not release_slot_permit(holder, holder_name, action=action, context=context):
            return False
        logger.warning(f"[SLOT_STALE_PERMIT] {context} for '{holder_name}' found a live "
                       f"permit; released it instead of orphaning it. "
                       f"An upstream release point was skipped.")
        return True

    @staticmethod
    def _stamp_llm_activity(instance: Any, event_type: str) -> None:
        """Stamp the holder's LLM-activity fields (FIFO head-stall alarm, plan §3.2).

        Called from the LLM-call generator at every ``_record_telemetry_event``
        boundary ('start'/'end') and per-chunk inside the streaming watchdog
        block. The signal is diagnostic-only: it tells the pool's wait loop
        whether the slot holder is actively producing output, so a stalled
        FIFO head can be distinguished from a legitimately long turn.

        Every write is getattr-guarded so test doubles without the fields keep
        working (same defensive pattern as slot_queue.py / core.py stale-permit
        sites). Failures are silently swallowed — this must never break an LLM call.
        """
        try:
            if not hasattr(instance, '_state_lock'):
                return
            with instance._state_lock:
                now = time.monotonic()
                if event_type == 'start':
                    instance._llm_call_active = True
                    instance._last_llm_activity = now
                elif event_type == 'end':
                    # The "last activity" of a finished call is its end time.
                    instance._llm_call_active = False
                    instance._last_llm_activity = now
                elif event_type == 'chunk':
                    # Write site B: every streamed chunk refreshes the timestamp;
                    # the active flag is untouched (set by 'start', cleared by 'end').
                    instance._last_llm_activity = now
        except Exception:
            pass

    @staticmethod
    def _acquire_scheduler_permit(router, instance: Any, slot_info: dict, holder_name: str, *,
                                  timeout: float, hard_cap: Optional[float] = None):
        """Acquire one scheduler permit with the caller's true endpoint identity.

        Centralizes the two otherwise-identical ``router.scheduler.acquire``
        calls in :meth:`reacquire_for` (fast window and FIFO-tail re-queue).
        BUG_0051 (F1): ``endpoint_name`` and ``model`` are passed through so the
        scheduler's acquire log names the real endpoint + model instead of
        falling back to first-match-by-base on a shared ``api_base`` config.
        ``hard_cap`` is omitted when None so the fast-path call keeps its exact
        original keyword surface.
        """
        kwargs = {
            'api_base': slot_info['api_base'],
            'concurrency_limit': slot_info['concurrency_limit'],
            'instance_name': holder_name,
            'agent_class': instance.agent_class,
            'endpoint_name': slot_info.get('endpoint_name'),
            'model': slot_info.get('model'),
            'timeout': timeout,
        }
        if hard_cap is not None:
            kwargs['hard_cap'] = hard_cap
        return router.scheduler.acquire(**kwargs)

    def reacquire_for(self, instance: Any, holder_name: str, context: str = 'reacquire') -> bool:
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
        from agent_cascade.slot_queue import SlotCancelled, SlotQueueTimeout

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
            slot_info = router.get_effective_slot_info(instance.agent_class, instance_name=holder_name)
        else:
            slot_info = router.get_agent_slot_info(instance.agent_class)
        if not slot_info or not slot_info.get('needs_slot'):
            # Unlimited endpoint — no slot to hold. Release any live permit instead of
            # discarding it: a nullify-without-release here orphans the SlotHolder in
            # pool._running exactly like LEAK #1, except with no warning at all.
            self._discard_stale_permit(instance, holder_name,
                                       context=f'{context} (unlimited endpoint)', action='drop-stale-unlimited')
            return True

        # Defensive fast-path: if the instance already holds a LIVE permit for exactly
        # the desired key, return without acquiring. SlotPool has no reentrant path —
        # an acquire() while holding the same capacity-1 pool self-deadlocks until the
        # timeout. This should never trigger under current usage (callers release first),
        # so a hit indicates unexpected usage and is logged as a warning.
        desired_key = slot_info.get('slot_key')
        with instance._state_lock:
            already_held = (getattr(instance, '_slot_key', None) == desired_key and
                            getattr(instance, '_slot_release', None) is not None)
        if already_held:
            logger.warning(f"[SLOTPOOL] instance={holder_name} pool={desired_key} "
                           f"action=sticky-keep (reacquire_for fast-path — unexpected: caller should "
                           f"have released first; skipping acquire to avoid self-deadlock)")
            return True

        try:
            # BUG_0051 (F1): pass the caller's TRUE endpoint identity so the scheduler's acquire log
            # line names the real endpoint + model. Without these, on a shared api_base config the
            # scheduler falls back to first-match-by-base (always the FIRST endpoint in file order) and
            # an empty model — misleading diagnostics even though the slot itself is correct. Both are
            # already computed by get_effective_slot_info (router.py:529-530); .get() tolerates None/''.
            release_cb = self._acquire_scheduler_permit(
                router, instance, slot_info, holder_name,
                timeout=REACQUIRE_TIMEOUT,  # module constant — bounded FAST re-acquire window
            )
            if release_cb is not None:
                with instance._state_lock:
                    instance._slot_release = release_cb
                    # Track which slot key this agent holds (for diagnostics).
                    instance._slot_key = slot_info.get('slot_key')
                # Best-effort last-released bookkeeping (outside the state lock): record the
                # endpoint this re-acquire resolved to so a later release updates
                # _last_released_endpoint even if the LLM call never ran. Never raises.
                self._safe_note_held_endpoint(router, instance.agent_class, holder_name)
                logger.debug(f"[SLOT_REACQUIRED] {context} - re-acquired slot for '{holder_name}'")
                return True
            else:
                # Unlimited — acquire returned None, no callback needed. Any permit the
                # caller was still holding is stale by definition; release, don't discard.
                self._discard_stale_permit(instance, holder_name,
                                           context=f'{context} (acquire returned None)',
                                           action='drop-stale-unlimited')
                return True
        except SlotCancelled:
            # Terminated mid-wait — propagate the clean abort (caller handles).
            raise
        except (SlotQueueTimeout, TimeoutError):
            # Fast-window timeout. The pool raises SlotQueueTimeout, but we always
            # acquire through EndpointScheduler.acquire, which re-raises it as a plain
            # TimeoutError (scheduler.py) — so catch both. Falling through here means:
            # re-enter the FIFO at the tail (bounded by POST_YIELD_REACQUIRE_TIMEOUT), below.
            pass

        # Sticky slot plan change #5b (user decision 3 / §3.9 Gap A): there is NO
        # slotless degraded state. The fast window above only bounds the post-yield
        # fast path; on timeout the instance re-enters the FIFO at the TAIL for its
        # resolved effective slot and blocks until granted — no bypass, no preemption.
        # The old [SLOT_REACQUIRE_FAILED] "degrade to async-only" path is deleted: it left a
        # conc=0 agent ungated, reintroducing the trashing window this project closes.
        #
        # Plan §3 A4: this tail re-queue passes LITERAL bounds, NOT _queue_limits() and NOT
        # the user's window. It does not pass `pool=`, so instance_resolver is None and the
        # dead-man's-switch window can NEVER reset here — which is precisely why it is
        # bounded explicitly. With hard_cap == timeout the sliding logic is inert (both
        # deadlines coincide, the cap fires first) so this is a plain fixed wait, unaffected
        # by the 6x derivation or by any UI setting.
        logger.info(f"[SLOTPOOL] instance={holder_name} pool={slot_info.get('slot_key')} "
                    f"action=acquire-queued waiters=-1 (post-yield fast re-acquire timed out after "
                    f"{REACQUIRE_TIMEOUT:.0f}s — re-entering FIFO at tail, bounded by "
                    f"{POST_YIELD_REACQUIRE_TIMEOUT:.0f}s)")
        try:
            # BUG_0051 (F1): same identity pass-through as the fast path above — the bug survives the
            # timeout branch unless BOTH reacquire sites carry endpoint_name + model.
            release_cb = self._acquire_scheduler_permit(
                router, instance, slot_info, holder_name,
                # Literal, deliberately NOT via _queue_limits — see the comment above.
                # Do NOT add `pool=self` here: that would silently re-enable the sliding
                # window on this path and reintroduce the unbounded-worker risk.
                timeout=POST_YIELD_REACQUIRE_TIMEOUT,
                hard_cap=POST_YIELD_REACQUIRE_TIMEOUT,
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
            # Best-effort last-released bookkeeping (outside the state lock): same as the fast
            # window above — record the endpoint this re-acquire resolved to so a later release
            # updates _last_released_endpoint even if the LLM call never ran. Never raises.
            self._safe_note_held_endpoint(router, instance.agent_class, holder_name)
            logger.debug(f"[SLOT_REACQUIRED] {context} - re-acquired slot for '{holder_name}' "
                         f"after unbounded FIFO wait")
            return True
        # Unlimited — acquire returned None, no callback needed. Any permit the
        # caller was still holding is stale by definition; release, don't discard.
        self._discard_stale_permit(instance, holder_name,
                                   context=f'{context} (acquire returned None)',
                                   action='drop-stale-unlimited')
        return True
