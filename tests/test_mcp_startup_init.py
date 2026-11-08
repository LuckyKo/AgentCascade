"""BUG_0269 — MCP servers must auto-connect at server startup via the shared boot path.

Covers ``agent_cascade.shared_init.initialize_mcp_servers``: it reads
``<project_root>/config/mcp_servers.json`` and drives the registered ``mcpServers``
config handler so enabled servers' tools land in every template's ``function_map`` at
boot (previously only a UI config push did this). Guards and the non-fatal contract
(never aborts boot) are covered explicitly.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent_cascade.config_handlers import CONFIG_HANDLERS
from agent_cascade.shared_init import initialize_mcp_servers


class FakeTemplate:
    """Stand-in for an Assistant/FnCallAgent template: only needs ``function_map``."""

    def __init__(self, name: str):
        self.name = name
        self.function_map = {}


def _make_pool():
    """Minimal fake pool exposing only ``.templates`` (a dict of template-like objects)."""
    return SimpleNamespace(templates={'coder': FakeTemplate('coder')})


def _write_config(project_root, content) -> None:
    cfg_dir = project_root / 'config'
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / 'mcp_servers.json').write_text(json.dumps(content), encoding='utf-8')


def test_registers_tool_into_template_function_map(tmp_path):
    """Real handler runs; mocked MCPManager.initConfig returns a tool that gets registered."""
    project_root = tmp_path
    _write_config(project_root, {'fake-server': {'enabled': True}})
    pool = _make_pool()
    fake_tool = SimpleNamespace(name='fake__do_thing')

    fake_mgr = MagicMock()
    fake_mgr.initConfig.return_value = [fake_tool]
    fake_mgr._connected_servers = {'fake-server': object()}

    # Both config_handlers._handle_mcp_servers and initialize_mcp_servers import
    # MCPManager from this module at call time, so patching the module attribute drives both.
    with patch('agent_cascade.tools.mcp_manager.MCPManager', return_value=fake_mgr):
        initialize_mcp_servers(pool, project_root)

    assert pool.templates['coder'].function_map.get('fake__do_thing') is fake_tool
    fake_mgr.initConfig.assert_called_once_with({'mcpServers': {'fake-server': {'enabled': True}}})


def test_noop_when_config_file_missing(tmp_path):
    """No config file → handler must not be called and boot continues (no crash)."""
    pool = _make_pool()
    mock_handler = MagicMock()
    with patch.dict(CONFIG_HANDLERS, {'mcpServers': mock_handler}):
        initialize_mcp_servers(pool, tmp_path)  # tmp_path/config/mcp_servers.json absent

    mock_handler.assert_not_called()
    assert pool.templates['coder'].function_map == {}


def test_noop_when_config_empty(tmp_path):
    """Empty config dict → handler must not be called."""
    project_root = tmp_path
    _write_config(project_root, {})
    pool = _make_pool()
    mock_handler = MagicMock()
    with patch.dict(CONFIG_HANDLERS, {'mcpServers': mock_handler}):
        initialize_mcp_servers(pool, project_root)

    mock_handler.assert_not_called()
    assert pool.templates['coder'].function_map == {}


def test_noop_when_no_handler_registered(tmp_path):
    """Config present but no 'mcpServers' handler → guard logs a warning and returns."""
    project_root = tmp_path
    _write_config(project_root, {'fake-server': {'enabled': True}})
    pool = _make_pool()
    # Replace CONFIG_HANDLERS with an empty dict: the function binds it at call time.
    with patch('agent_cascade.config_handlers.CONFIG_HANDLERS', {}):
        initialize_mcp_servers(pool, project_root)  # must not raise

    assert pool.templates['coder'].function_map == {}


def test_handler_exception_is_swallowed(tmp_path):
    """A raising handler must NOT propagate — boot continues (best-effort contract)."""
    project_root = tmp_path
    _write_config(project_root, {'fake-server': {'enabled': True}})
    pool = _make_pool()
    raising = MagicMock(side_effect=RuntimeError('MCP server exploded'))

    with patch.dict(CONFIG_HANDLERS, {'mcpServers': raising}):
        initialize_mcp_servers(pool, project_root)  # must return normally, not raise

    raising.assert_called_once()
