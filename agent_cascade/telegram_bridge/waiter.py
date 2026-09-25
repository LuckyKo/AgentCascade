"""Per-task completion waiter.

After a task is injected into AC, this coroutine polls ``GET /api/status`` until
``generating`` flips to false (the run thread sets it on natural completion), or
until the per-task timeout elapses. It then fetches the root agent's final answer
from the open ``GET /api/state`` endpoint.

The session token is obtained fresh on each poll via ``client.ensure_token()`` so
that an AC restart mid-wait (which invalidates the in-memory, no-TTL token) is
recovered automatically: a 401 from ``get_status`` invalidates the cache, and the
next iteration re-handshakes.

v1 limitation (documented in the plan): if AC was already generating before our
message, we simply wait for the next transition to false and attribute that final
message to us. This is "correct enough" for a single-operator bridge; tracking a
monotonic generation_id is future work.
"""

import asyncio
from typing import Any, Dict, List, Optional

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


def extract_final_message(state: Dict[str, Any]) -> str:
    """Return the text of the LAST assistant message in ``state['messages']``.

    ``serialize_message`` normally normalizes multimodal list content to a string,
    but we guard defensively: if content is still a list, extract the text parts;
    if it's neither str nor list, coerce with str(). Returns '' when there is no
    assistant message (caller decides how to surface that).
    """
    messages = state.get('messages') or []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get('role') != 'assistant':
            continue
        content = msg.get('content', '')
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, dict) and 'text' in item:
                    parts.append(str(item['text']))
                elif isinstance(item, str):
                    parts.append(item)
            return ''.join(parts)
        # Non-empty non-str/non-list (e.g. number) -> coerce; None/empty -> keep looking.
        if content is not None and content != '':
            return str(content)
    return ''


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


async def fetch_final_message(client: ACClient) -> str:
    """Fetch /api/state and return the root agent's final assistant message text."""
    state = await client.get_state()
    return extract_final_message(state)
