"""Per-task completion waiter.

After a task is injected into AC, this coroutine polls ``GET /api/status`` until
``generating`` flips to false (the run thread sets it on natural completion), or
until the per-task timeout elapses. Final-answer delivery itself is push-based at
run end (see plans/tg-bridge-push-model_PLAN.md) — the waiter only provides
progress/offline notices and exits quietly on FINISHED.

The session token is obtained fresh on each poll via ``client.ensure_token()`` so
that an AC restart mid-wait (which invalidates the in-memory, no-TTL token) is
recovered automatically: a 401 from ``get_status`` invalidates the cache, and the
next iteration re-handshakes.

v1 limitation (documented in the plan): if AC was already generating before our
message, we simply wait for the next transition to false. This is "correct enough"
for a single-operator bridge; tracking a monotonic generation_id is future work.
"""

import asyncio
from typing import Optional

import httpx

from agent_cascade.log import logger
from agent_cascade.settings import TG_POLL_INTERVAL_SEC, TG_TASK_TIMEOUT_SEC

from .ac_client import ACClient, ACError


class WaiterResult:
    """Outcome of waiting for completion."""

    FINISHED = 'finished'      # generating flipped to false (or was already idle)
    TIMEOUT = 'timeout'        # still generating after the deadline
    OFFLINE = 'offline'        # AC unreachable for too long

    def __init__(self, status: str):
        self.status = status


async def wait_for_completion(
    client: ACClient,
    poll_interval: float = TG_POLL_INTERVAL_SEC,
    timeout: float = TG_TASK_TIMEOUT_SEC,
    offline_after: Optional[float] = None,
) -> WaiterResult:
    """Poll /api/status until ``generating`` is false, or the deadline passes.

    Returns a :class:`WaiterResult`:
      - FINISHED: generation ended (or was already idle).
      - TIMEOUT:  still generating after ``timeout`` seconds.
      - OFFLINE:  AC unreachable for longer than ``offline_after`` seconds
                  (defaults to ``timeout``, i.e. an offline AC simply times out;
                  pass a smaller value to surface "AC appears offline" sooner).

    The token is re-resolved on each poll so an AC restart mid-wait is recovered
    via the client's 401 -> re-handshake path. Transient errors are tolerated and
    retried until the deadline.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    offline_deadline = (loop.time() + (offline_after if offline_after is not None else timeout))
    poll_interval = max(0.1, float(poll_interval))

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.warning('wait_for_completion timed out after %.0fs', timeout)
            return WaiterResult(WaiterResult.TIMEOUT)

        try:
            token = (await client.ensure_token())[0]
            status = await client.get_status(token)
            offline_deadline = loop.time() + (offline_after if offline_after is not None else timeout)
            if not status.get('generating', False):
                return WaiterResult(WaiterResult.FINISHED)
        except (ACError, asyncio.TimeoutError, OSError, httpx.HTTPError) as e:
            # Tolerate transient failures; keep polling until the deadline. If AC has
            # been unreachable for longer than offline_after, report OFFLINE early.
            logger.debug('wait_for_completion poll error (will retry): %s', e)
            if loop.time() > offline_deadline:
                logger.warning('AC appears offline (unreachable > %.0fs)', offline_after or timeout)
                return WaiterResult(WaiterResult.OFFLINE)

        await asyncio.sleep(min(poll_interval, remaining))
