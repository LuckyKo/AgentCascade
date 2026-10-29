"""Termination-check + interruptible-sleep helpers (moved verbatim from api_router.py)."""

import time

from agent_cascade.exceptions import AgentTerminatedError


class _AppLoggerProxy:
    """Late-bound delegate to agent_cascade.log.logger (same pattern as slot_queue/scheduler)."""
    __slots__ = ()

    def __getattr__(self, name):
        from agent_cascade.log import logger as _live
        return getattr(_live, name)


logger = _AppLoggerProxy()


def _check_termination(pool, instance_name: str) -> bool:
    """Check if an instance has been terminated/dismissed.

    Returns True if the instance should abort its current operation.
    Safe to call with None pool or empty instance_name — returns False.
    """
    if not pool or not instance_name:
        return False
    return pool.is_instance_terminated(instance_name)


def _stop_requested(pool) -> bool:
    """Pool-level user Stop. Single source of truth for the stop signal in the router:
    reads the existing AgentPool.stopped property (pool/core.py) over the pool's own
    _stopped_event — no new flag, event or counter. Strict on purpose: pool doubles
    (MagicMock) expose truthy auto-attributes; only a real True counts. Safe with pool=None."""
    return getattr(pool, 'stopped', False) is True


def _should_abort_lockfree(pool, instance_name: str) -> bool:
    """Cancellation for the router's loop-top guards (G1-G4). Deliberately lock-free: the
    membership term mirrors the membership test the guards previously inlined in router.py
    and does NOT call pool.is_instance_terminated(), which takes pool._pool_lock and
    additionally tests inst.is_terminated. The getattr keeps this safe for pool doubles that
    lack terminated_instances (so a double without the attr cannot crash at the entry guard)."""
    if _stop_requested(pool):
        return True
    terminated = getattr(pool, 'terminated_instances', None)
    return bool(terminated and instance_name and instance_name in terminated)


def _should_abort_locked(pool, instance_name: str) -> bool:
    """Cancellation for _interruptible_sleep's tick: user Stop OR the full per-instance
    termination check. This is the LOCK-TAKING variant: it calls is_instance_terminated(),
    which acquires pool._pool_lock. That path already pays that lock on every tick today,
    so keeping the full check here costs no new lock. (The loop-top guards use the lock-free
    _should_abort_lockfree instead — do NOT call this one on a hot path.)"""
    if _stop_requested(pool):
        return True
    return _check_termination(pool, instance_name)


def _interruptible_sleep(duration: float, pool, instance_name: str, interval: float = 0.5) -> None:
    """Sleep for duration seconds, checking termination or stop every interval seconds.

    Raises AgentTerminatedError if the instance is terminated or a pool-level Stop is
    requested during the wait.
    """
    start = time.monotonic()
    while time.monotonic() - start < duration:
        if _should_abort_locked(pool, instance_name):
            raise AgentTerminatedError(instance_name or 'unknown')
        remaining = duration - (time.monotonic() - start)
        time.sleep(min(interval, remaining))
