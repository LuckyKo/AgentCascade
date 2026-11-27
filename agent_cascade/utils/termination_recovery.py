"""Scoped re-arm of a loop-terminated main agent (BUG_0205).

When the two-tier loop detector exhausts its auto-rollback budget it terminates the
instance (state -> TERMINATED, ``is_terminated=True``, added to
``pool.terminated_instances``). For a root/main agent nothing else ever clears that
marker, so the next user-initiated run trips the L1 race guard (``engine.run()``
requires ``state == IDLE``) and dies before turn 1.

This helper re-arms ONLY an instance whose termination was recorded as recoverable —
i.e. the loop detector wrote a machine-readable reason onto it
(``inst._terminated_reason``, set in ``engine/llm_call.py``). Dismissals, cascade
teardowns, and user Stops leave ``_terminated_reason`` unset, so they stay dead
(the "TERMINATED must stay dead" contract).

Mirrors ``_reset_terminated_compressor_for_retry`` (BUG_0001,
``compression/agent_invoker.py``). Lock order: per-instance ``_state_lock``, released
BEFORE ``pool._pool_lock`` — never nested (lifecycle_manager.py:294 "Gotcha 3").
"""

from agent_cascade.agent_instance import AgentState
from agent_cascade.log import logger


def rearm_terminated_instance(pool, instance_name: str, origin: str, force: bool = False) -> bool:
    """TERMINATED -> IDLE for a root instance still present in the pool (BUG_0205).

    Only re-arms a ROOT instance (``parent_instance is None``) whose loop-termination
    reason was recorded by the loop detector (``inst._terminated_reason`` is a
    non-empty str). Child instances (``parent_instance`` set) are NOT re-armed here —
    they are managed by the child_runner dismissal path. Clears both the instance
    flag and the pool-level termination marker, resets the loop state, and logs a
    greppable WARNING. On success it CONSUMES the one-shot recoverable marker
    (clears ``_terminated_reason``) so a later non-loop termination stays dead.

    Precondition: no ``engine.run()`` thread is in flight for this instance at call
    time (mirrors BUG_0001). The flag clear (``is_terminated=False`` under
    ``_state_lock``) and the pool-set discard (under ``_pool_lock``) are two separate
    lock scopes — a small window exists where ``is_instance_terminated`` is still True
    while the instance is already IDLE. Acceptable because re-arm only fires on a
    user-initiated run with no in-flight thread for that instance.

    BUG_0308 exception to the above: a DMS wedge-termination (``_terminated_reason`` carrying the
    ``dms-wedge:`` marker) can leave its engine.run() thread parked (abandoned un-joined), so this
    helper re-arms such a reason ONLY when ``_in_blocking_tool is None`` (the parked thread has
    unwound). While it is still parked the re-arm is deferred (returns False); a later user message
    retries. Loop-detector reasons and reason-less terminations are unaffected by this gate.

    BUG_0431: when ``force=True`` (an explicit user-message resume, where pool._run_generation has
    already been bumped so any parked thread is generation-superseded), the quiescence gate is
    SKIPPED and a stale ``_in_blocking_tool`` marker is cleared — the durable generation gate, not
    the marker, is what protects the live run. ``force`` does NOT bypass the reason gate below.

    Args:
        pool: The AgentPool (provides ``get_instance``, ``terminated_instances``,
            ``_pool_lock``).
        instance_name: The instance to re-arm.
        origin: Caller label for the log line (e.g. ``"user-message"``).
        force: Skip the DMS-wedge parked-thread quiescence gate and clear any stale
            ``_in_blocking_tool`` marker (BUG_0431). Default False preserves BUG_0308 deferral.
            Does NOT lift the reason gate (a reason-less termination still stays dead).

    Returns:
        True when re-armed; False when the instance is absent, is a child (has a
        parent), is not terminated, or was terminated for a non-recoverable reason
        (dismissal, cascade teardown, user Stop) — in which case it is left dead.
    """
    # Normalize the name (mirrors terminate_instance, pool/lifecycle.py:232):
    # pool.get_instance() strips the name, but the discard below must use the
    # same normalized form — a padded name would leave the set entry behind.
    instance_name = instance_name.strip()

    inst = pool.get_instance(instance_name)
    if inst is None:
        return False

    # Root-agent only: children are managed by the child_runner dismissal path.
    if getattr(inst, 'parent_instance', None) is not None:
        return False

    # Only loop-terminations are recoverable; dismissals/teardowns/Stop leave this unset.
    reason = getattr(inst, '_terminated_reason', None)
    if not isinstance(reason, str) or not reason:
        # Diagnostic only for a genuinely-dead root: a healthy IDLE instance also has no reason and
        # returns False on every user message — that is the normal no-op path, not a refusal to log.
        if getattr(inst, 'is_terminated', False) or getattr(inst, 'state', None) == AgentState.TERMINATED:
            logger.warning(
                f"[REARM] '{instance_name}' re-arm REFUSED (reason gate): no recoverable "
                f"_terminated_reason (value={reason!r}) — 'TERMINATED stays dead' (origin={origin}, force={force})."
            )
        return False

    # BUG_0308: a DMS wedge-termination records a recoverable reason, but the wedged root's
    # engine.run() thread may still be PARKED inside its blocking tool (terminate_instance abandons
    # it WITHOUT joining — BUG_0244). Re-arming TERMINATED->IDLE while that thread lives re-introduces
    # the double-transition / orphaned-permit race the precondition above exists to prevent.
    #
    # QUIESCENCE GATE (defense-in-depth): a wedge reason re-arms only once the parked thread has
    # unwound. The dispatch-boundary `finally` (tool_dispatcher.py) sets _in_blocking_tool=None on
    # unwind, so None proves the thread is no longer stuck in the blocking primitive.
    #
    # The DURABLE protection is elsewhere and does NOT rely on is_instance_terminated (which this
    # re-arm itself clears below): a fresh run bumped pool._run_generation BEFORE reaching this re-arm
    # (run_agent_unified.py bumps _run_generation; fresh ExecutionEngine per call at runner.py), so the stale thread
    # is generation-superseded and _finalize_state_transition (run_loop.py) refuses to let it touch the
    # live run's shared state. The two gates are complementary: quiescence defers re-arm while the
    # thread is parked; the generation gate closes the residual window where the marker is already
    # cleared but the thread has not yet finalized. A reason-LESS termination (Stop/dismissal) already
    # returned above, so the "TERMINATED stays dead" contract is unchanged. A genuinely forever-stuck
    # thread correctly stays dead (reviving it would double-run).
    from agent_cascade.pool.idle_manager import is_dms_wedge_reason
    # BUG_0431: ``force`` (user-message resume, _run_generation already bumped) lifts this deferral;
    # the stale marker is cleared in the reset block below. Without force, defer (BUG_0308).
    if not force and is_dms_wedge_reason(reason) and getattr(inst, '_in_blocking_tool', None) is not None:
        logger.warning(
            f"[REARM] '{instance_name}' wedge re-arm DEFERRED: parked thread still in blocking tool "
            f"(origin={origin}); will re-arm once it unwinds."
        )
        return False

    # Per-instance lock first, released BEFORE the pool lock (never nested — Gotcha 3).
    with inst._state_lock:
        if inst.state == AgentState.TERMINATED:
            # TERMINATED has no valid transitions out (agent_instance.py state matrix),
            # so assign directly rather than via _transition().
            inst.state = AgentState.IDLE
        # An instance terminated while IDLE keeps state==IDLE + is_terminated=True
        # (AgentInstance.terminate() only transitions from ACTIVE_STATES), so clear
        # the flag unconditionally — the state check above is conditional, not a guard.
        inst.is_terminated = False
        # Reset loop state so the re-armed run gets a fresh rollback budget (all three
        # fuzzy fields, matching the other reset sites in engine/llm_call.py).
        # _loop_rollback_count is also reset by _run_preflight (engine/run_loop.py:546);
        # kept here defensively in case a future preflight change drops it.
        inst._loop_rollback_count = 0
        inst._fuzzy_warn_armed = True
        inst._fuzzy_escalation_armed = False
        inst._fuzzy_warn_last_turn = -10**9
        # Consume the one-shot recoverable marker NOW that we've acted on it. A LATER
        # non-loop termination (dismiss / teardown / Stop) must stay dead, so the reason
        # is cleared; a fresh loop termination re-sets it via engine/llm_call.py.
        inst._terminated_reason = None
        # BUG_0431: a forced (user-message) re-arm discards a possibly-stale _in_blocking_tool
        # marker. The parked thread is generation-superseded (run_agent_unified bumped
        # _run_generation before reaching here), so it can't touch the live run's state; the new
        # run re-sets the marker on tool entry. Only on force — a non-forced re-arm leaves the
        # marker as-is (prior BUG_0205/0308 behavior: a wedge marker was proven None by the gate).
        if force:
            inst._in_blocking_tool = None

    with pool._pool_lock:
        pool.terminated_instances.discard(instance_name)

    logger.warning(
        f"[REARM] '{instance_name}' -> IDLE for new run (BUG_0205): {reason} (origin={origin})"
    )
    return True
