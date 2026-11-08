"""BUG_0271 — MCP boot path must NOT connect servers whose 'enabled' flag is False.

Covers ``agent_cascade.tools.mcp_manager.MCPManager.init_config_async`` (the single
connection entry point used by the boot path via the ``mcpServers`` handler). The per-server
``enabled`` flag is a UI concern persisted to ``config/mcp_servers.json``; ``init_config_async``
must skip disabled servers (defaulting to True when the key is absent) before connecting.

No real MCP connection is made: ``MCPClient`` and the tool-building path are mocked.

Run serially (pytest.ini pins xdist):
    python -m pytest tests/test_mcp_enabled_filter.py -q -n 0
"""

import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure top-level imports work (mirror tests/test_mcp_media_paths.py convention).
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.tools.mcp_manager import MCPManager


def _make_mgr() -> MCPManager:
    """A fresh MCPManager that bypasses the singleton __new__ and __init__.

    __init__ would start a daemon event-loop thread and monkey-patch mcp — irrelevant here.
    Set only the attributes init_config_async touches (see [[test-instance-defaults-bypass]]).
    """
    mgr = object.__new__(MCPManager)
    mgr.clients: dict = {}
    mgr._connected_servers: dict = {}  # server_name:fingerprint -> client_id
    mgr._server_tools: dict = {}
    mgr._mcp_lock = threading.Lock()
    return mgr


def _connected_names(mgr: MCPManager) -> set:
    """Server names present in _connected_servers (keys are 'name:fingerprint')."""
    return {key.split(':', 1)[0] for key in mgr._connected_servers}


class TestInitConfigAsyncEnabledFilter:
    @pytest.mark.asyncio
    async def test_only_enabled_and_flagless_servers_connect(self):
        mgr = _make_mgr()
        config = {'mcpServers': {
            'enabled-server': {'enabled': True, 'command': 'cmd'},
            'disabled-server': {'enabled': False, 'command': 'cmd'},
            'flagless-server': {'command': 'cmd'},  # no 'enabled' key -> default True
        }}

        with patch('agent_cascade.tools.mcp_manager.MCPClient') as McpClientMock:
            # The client instance whose connection_server we track and mock.
            client_inst = McpClientMock.return_value
            client_inst.connection_server = AsyncMock(return_value=None)
            # Mock the tool-building path so no real MCP session is exercised.
            mgr._build_tools_for_client = AsyncMock(side_effect=lambda *a, **k: [])

            await mgr.init_config_async(config)

            connected = {
                c.kwargs.get('mcp_server_name')
                for c in client_inst.connection_server.await_args_list
            }

        # Only the enabled + flagless servers should reach connection_server.
        assert connected == {'enabled-server', 'flagless-server'}, (
            f"Expected only enabled/flagless to connect, got {connected}"
        )
        # The disabled server must never appear in the connected-state map.
        assert 'disabled-server' not in _connected_names(mgr)
        # Sanity: the two expected servers ARE marked connected.
        assert {'enabled-server', 'flagless-server'} <= _connected_names(mgr)

    @pytest.mark.asyncio
    async def test_no_server_key_defaults_true_and_connects(self):
        # Single flagless server connects (guards the default-True behaviour on its own).
        mgr = _make_mgr()
        with patch('agent_cascade.tools.mcp_manager.MCPClient') as McpClientMock:
            client_inst = McpClientMock.return_value
            client_inst.connection_server = AsyncMock(return_value=None)
            mgr._build_tools_for_client = AsyncMock(side_effect=lambda *a, **k: [])

            await mgr.init_config_async({'mcpServers': {'solo': {'command': 'cmd'}}})

        assert _connected_names(mgr) == {'solo'}
