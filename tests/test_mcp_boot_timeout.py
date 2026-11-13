"""BUG_0310 R1 — MCP connect must be time-bounded, never hang the boot thread.

Covers two things:
  1. ``MCPManager.initConfig`` waits on ``future.result(timeout=MCP_CONNECT_TIMEOUT_S)``
     so a slow / hung MCP connect raises ``concurrent.futures.TimeoutError`` after a
     BOUNDED wait (not the full, unbounded time the fake coroutine sleeps). This proves
     the boot thread can no longer be stuck forever on a dead ``npx``.
  2. The exception ``initConfig`` now raises on timeout is an ordinary ``Exception``, so
     the EXISTING handled path ``config_handlers._handle_mcp_servers`` swallows it and
     boot continues — i.e. no new crash path was introduced.

Both tests drive the real code with only tiny, local fakes (a stubbed ``init_config_async``
and a throwaway event loop); the heavy real ``MCPManager.__init__`` (mcp import, loop
thread, monkey-patch) is bypassed via ``object.__new__``.
"""

import asyncio
import concurrent.futures
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.config_handlers import CONFIG_HANDLERS
from agent_cascade.tools import mcp_manager
from agent_cascade.tools.mcp_manager import MCPManager


# How long the fake MCP connect "sleeps" — must exceed the capped wait so the timeout
# branch fires (and the assert below can prove we returned before it would finish).
STUB_CONNECT_SECONDS = 5.0


def _make_bare_manager(loop: asyncio.AbstractEventLoop) -> MCPManager:
    """A MCPManager that skips the real __init__ (mcp import + loop thread + monkey-patch).

    object.__new__ bypasses the singleton __new__, so we get a fresh instance; we only
    wire up what initConfig touches: .loop (a RUNNING loop) and a stubbed async init.
    """
    mgr = object.__new__(MCPManager)
    mgr.loop = loop
    return mgr


def test_initconfig_bounded_timeout_raises_after_bounded_wait(monkeypatch):
    """A 5s fake connect with a 0.2s cap must raise in ~0.2s, NOT after 5s."""
    timeout = 0.2
    # initConfig reads this module-global at call time.
    monkeypatch.setattr(mcp_manager, 'MCP_CONNECT_TIMEOUT_S', timeout)

    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    mgr = _make_bare_manager(loop)

    async def slow_init_config_async(config):  # mimics a hung npx spawn + handshake
        await asyncio.sleep(STUB_CONNECT_SECONDS)
        return []  # never reached within the cap

    mgr.init_config_async = slow_init_config_async
    config = {'mcpServers': {'sleepy': {'command': 'sleepy', 'args': []}}}

    start = time.perf_counter()
    with pytest.raises(concurrent.futures.TimeoutError):
        mgr.initConfig(config)
    elapsed = time.perf_counter() - start

    try:
        # Proof it is bounded: raised well before the stubbed connect would finish.
        assert elapsed < STUB_CONNECT_SECONDS * 0.5, (
            f'initConfig waited {elapsed:.2f}s, expected bounded by {timeout}s — '
            'the timeout was not applied')
    finally:
        # Let the abandoned sleep task end cleanly, then stop the loop (daemon thread,
        # so it can never block interpreter exit either way).
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=1.0)


def test_timeout_flows_into_config_handler_no_crash():
    """The TimeoutError that initConfig now raises is swallowed by _handle_mcp_servers.

    Mirrors exactly what real initConfig produces on a bounded timeout, then runs the
    registered handler — which must NOT propagate (boot continues WITHOUT MCP).
    """
    raised = concurrent.futures.TimeoutError('MCP connect exceeded MCP_CONNECT_TIMEOUT_S')
    fake_mgr = MagicMock()
    fake_mgr.initConfig.side_effect = raised

    handler = CONFIG_HANDLERS['mcpServers']
    with patch('agent_cascade.tools.mcp_manager.MCPManager', return_value=fake_mgr):
        # Must return None (not raise) — the existing handled-failure path.
        result = handler({'mcpServers': {'sleepy': {'enabled': True}}}, None, [])

    assert result is None
    fake_mgr.initConfig.assert_called_once_with(
        {'mcpServers': {'sleepy': {'enabled': True}}})
