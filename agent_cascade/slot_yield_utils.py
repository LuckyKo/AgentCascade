"""Shared slot-yield helpers for the Security and Compression invokers.

Extracted from security_handler.py (SecurityAdvisorHandler) and
compression/agent_invoker.py so the three-path slot-yield logic and the
pool-holder diagnostic are defined once instead of duplicated nearly verbatim.

This module intentionally depends ONLY on stdlib + logging — it takes the
agent pool / engine as parameters, so no agent_cascade imports are needed and
there is no circular-import risk.

KV save/restore is INJECTED, not imported: callers pass a ``save_fn`` callback
(an ExecutionEngine method) that persists the caller's KV state before the slot
is released. This keeps the module stdlib-only (no state_ops import) while making
every yield caller eviction-safe by construction.
"""
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def describe_pool_holders(agent_pool: Any, caller_name: str) -> str:
    """Short string of current holders on the caller's endpoint pool for slot diagnostics.

    Low-cost and non-blocking; returns a short 'n/a'/'error' marker on any failure.
    Mirrors SecurityAdvisorHandler._describe_pool_holders (security_handler.py).
    """
    try:
        router = agent_pool.api_router
        if not router or not caller_name:
            return 'n/a (no router)'
        inst = agent_pool.get_instance(caller_name)
        agent_class = inst.agent_class if inst else None
        if not agent_class:
            return 'n/a (caller instance gone)'
        slot_info = router.get_agent_slot_info(agent_class)
        if not slot_info or not slot_info.get('needs_slot'):
            return 'none (unlimited endpoint — no slot needed)'
        sched_pool = router.scheduler._get_or_create_pool(
            slot_info['api_base'], slot_info['concurrency_limit']
        )
        holders = [
            f"{h.instance_name} ({h.agent_name})"
            for h in sched_pool._running.values()
        ]
        return ', '.join(holders) if holders else 'empty'
    except Exception as e:
        return f"error ({e})"


def yield_caller_slot(
    agent_pool: Any,
    engine: Any,
    caller_inst: Any,
    caller_name: str,
    *,
    log_prefix: str,
    release_reason: str,
    before_action: str,
    save_fn: Optional[Callable[[Any, str], bool]] = None,
) -> bool:
    """Yield the caller's endpoint slot before running a system-launched agent.

    The system-launched agent (Security / Compressor) acquires its OWN endpoint
    slot (no borrowing), so the caller must free its permit first or it deadlocks
    on the shared sequential slot. Three distinct paths:
      1. Normal yield   — caller holds a live _slot_release callback.
      2. Force-release  — callback was cleared but the pool still shows the caller
                          holding a permit (leaked/stale state).
      3. Skip           — nothing to yield (pool already free); still persist KV so a
                           later restore has something to consume, then log diagnostic.

    Args:
        agent_pool: The AgentPool instance (used for pool diagnostics / force-release).
        engine: The ExecutionEngine (provides _release_slot()).
        caller_inst: The resolved caller AgentInstance, or None if it is gone.
        caller_name: The caller's instance name (used in log messages and as the
            holder key in the pool).
        log_prefix: Log tag prefix, e.g. "SECURITY_SLOT_YIELD" / "COMPRESSION_SLOT_YIELD".
            The skip diagnostic uses f"{log_prefix}_SKIPPED]".
        release_reason: String passed to engine._release_slot() (e.g. "before_security_check").
        before_action: Human-readable action phrase for the Path-1 log
            (e.g. "Security check" / "compression").
        save_fn: Optional injected callback ``(instance, reason) -> bool`` that persists
            the caller's KV state BEFORE the slot is released (eviction-safety). Called on
            all three paths — Path 1 and Path 2 immediately before the release, and Path 3
            (no slot to yield) so a later restore still has something to consume — inside
            the existing try/logging discipline. Production callers pass
            ``engine.save_before_slot_yield``; it self-gates for non-autoloader instances
            and never raises. When None (default) no save is performed — preserving the
            legacy behavior for any caller that has not opted in.

    Returns:
        True if a slot was yielded (so the caller knows to reacquire in its finally
        block), else False. If caller_inst is None, returns False with no logging —
        matching the callers' `if caller_inst:` guard.
    """
    if not caller_inst:
        return False

    if getattr(caller_inst, '_slot_release', None) is not None:
        # Path 1 — normal yield via the live release callback.
        logger.debug(
            f"[{log_prefix}] Releasing slot for '{caller_name}' before {before_action}"
        )
        # Persist KV state BEFORE releasing (eviction-safety): at this instant the caller
        # still owns the endpoint, so the save lands on its held endpoint and the label
        # survives any model swap that happens while it is slotless. Best-effort; never
        # raises (save_fn is engine.save_before_slot_yield, which swallows errors).
        if save_fn is not None:
            try:
                save_fn(caller_inst, release_reason)
            except Exception as e:  # noqa: BLE001 — a missed save degrades to legacy behavior
                logger.debug(f"[{log_prefix}] KV save before yield failed for '{caller_name}': {e}")
        # Structured drop-handoff event (sticky slot plan change #9/#10c): system agents
        # (Security/Compressor) use the same yield/reacquire path as sync children.
        engine._release_slot(caller_inst, caller_name, release_reason, action='drop-handoff')
        return True

    # Callback is None. Check whether the pool STILL shows the caller as a
    # holder — if so the permit leaked (callback cleared without releasing).
    _leaked_holder = None
    try:
        router = agent_pool.api_router
        slot_info = router.get_agent_slot_info(caller_inst.agent_class)
        if slot_info and slot_info.get('needs_slot'):
            sched_pool = router.scheduler._get_or_create_pool(
                slot_info['api_base'], slot_info['concurrency_limit']
            )
            _leaked_holder = sched_pool._running.get(caller_name)
    except Exception as e:
        logger.debug(
            f"[{log_prefix}] Failed to inspect pool for '{caller_name}': {e}"
        )

    if _leaked_holder is not None:
        # Path 2 — force-release the leaked permit directly from the pool.
        logger.warning(
            f"[{log_prefix}] LEAKED PERMIT DETECTED for '{caller_name}' "
            f"— force-releasing"
        )
        try:
            # Persist KV state BEFORE the force-release too (eviction-safety): the caller is
            # about to be slotless, so save first. Same best-effort discipline as Path 1.
            if save_fn is not None:
                try:
                    save_fn(caller_inst, release_reason)
                except Exception as e:  # noqa: BLE001 — a missed save degrades to legacy behavior
                    logger.debug(f"[{log_prefix}] KV save before force-release failed for '{caller_name}': {e}")
            sched_pool.release(_leaked_holder)
            # release() is silent on stale/no-op (returns None), so verify
            # the holder actually left the pool before flagging a yield —
            # otherwise we'd wrongly reacquire and leave the pool inconsistent.
            if _leaked_holder.instance_name not in sched_pool._running:
                return True
            else:
                logger.error(
                    f"[{log_prefix}] Force-release did not remove "
                    f"holder for '{caller_name}' — leaving slot as-is"
                )
        except Exception as e:
            logger.error(
                f"[{log_prefix}] Force-release check failed for "
                f"'{caller_name}': {e}", exc_info=True
            )
        return False

    # Path 3 — no slot to yield; still persist KV so a later restore has something
    # to consume. Without this the whole save/restore pair was a silent no-op whenever
    # the caller is already slotless. Best-effort; never raises (same discipline as Paths 1/2).
    # The success is logged at INFO (unlike Paths 1/2) because this skip path used to be
    # invisible — operators need to see that a save actually happened here.
    if save_fn is not None:
        try:
            save_fn(caller_inst, release_reason)
            logger.info(f"[{log_prefix}] KV saved on skip-path for '{caller_name}' (no slot to yield)")
        except Exception as e:  # noqa: BLE001 — a missed save degrades to legacy behavior
            logger.debug(f"[{log_prefix}] KV save on skip-path failed for '{caller_name}': {e}")
    logger.debug(
        f"[{log_prefix}_SKIPPED] No slot to yield for "
        f"'{caller_name}' — Pool holders: {describe_pool_holders(agent_pool, caller_name)}"
    )
    return False
