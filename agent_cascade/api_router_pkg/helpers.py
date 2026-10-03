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


def _interruptible_sleep(duration: float, pool, instance_name: str, interval: float = 0.5) -> None:
    """Sleep for duration seconds, checking termination every interval seconds.

    Raises AgentTerminatedError if the instance is terminated during the wait.
    """
    start = time.monotonic()
    while time.monotonic() - start < duration:
        if _check_termination(pool, instance_name):
            raise AgentTerminatedError(instance_name)
        remaining = duration - (time.monotonic() - start)
        time.sleep(min(interval, remaining))
