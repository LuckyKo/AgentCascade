"""BUG_0310 follow-up — lazy / on-use MCP server connect.

Covers the lazy mode added to ``MCPManager`` (park at boot / connect on first use /
all-disabled→off), the dispatcher/tool_search triggers' shared ``ensure_connected``
single-flight, the ``connect_server`` timeout parameterization + stub overwrite, and the
cold-cache background thread. The eager kill-switch is covered by ``test_knob_off_eager``
(shared_init routing).

No real MCP connection or subprocess is made: ``MCPClient`` and the tool-build coroutine are
mocked, and ``asyncio.run_coroutine_threadsafe`` is replaced with a synchronous runner so no
event-loop thread is needed (the manager under test is built with ``object.__new__`` and
``loop=None``).

Run serially (pytest.ini pins xdist):
    python -m pytest tests/test_mcp_lazy_connect.py -q -n 0
"""

import asyncio
import concurrent.futures
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

import agent_cascade.tools.mcp_manager as mcp_manager
from agent_cascade.tools.mcp_manager import MCPManager


@pytest.fixture(autouse=True)
def _clean_registered_tool_names():
    """_registered_tool_names is CLASS state shared across every manager instance — reset it
    so stub registration in one test cannot make another test's is_mcp_tool() true."""
    MCPManager._registered_tool_names.clear()
    yield
    MCPManager._registered_tool_names.clear()


def _make_mgr() -> MCPManager:
    """A fresh MCPManager that bypasses __new__ (singleton) and __init__ (event loop thread).

    Sets the eager attributes AND all lazy-connect state the new code reads/writes. ``loop`` is
    None: connect/disconnect's run_coroutine_threadsafe is patched to a synchronous runner in
    the tests that need it, which ignores the loop argument.
    """
    mgr = object.__new__(MCPManager)
    mgr.clients: dict = {}
    mgr._connected_servers: dict = {}
    mgr._server_tools: dict = {}
    mgr._mcp_lock = threading.Lock()
    mgr.loop = None
    mgr._parked_servers: dict = {}
    mgr._connecting: dict = {}
    mgr._suppressed: set = set()
    mgr._config_disabled: set = set()
    mgr._tool_server_map: dict = {}
    return mgr


class _FakeTpl:
    def __init__(self):
        self.function_map: dict = {}
        self.agent_type = ''


class _FakePool:
    """Minimal pool: templates_map + a configurable get_ui_disabled_tools_for_agent."""

    def __init__(self, templates_map: dict, disabled_map: dict | None = None):
        self.templates = templates_map
        self._disabled = disabled_map or {}
        self._marked = []

    def get_ui_disabled_tools_for_agent(self, agent_name: str, agent_type: str = '') -> set:
        return set(self._disabled.get(agent_name, set()))

    def _mark_activity(self, name):
        self._marked.append(name)


def _live_tool(name: str):
    return SimpleNamespace(
        name=name, description=f'desc for {name}',
        parameters={'type': 'object', 'properties': {}, 'required': []},
    )


def _patch_rcts():
    """Replace run_coroutine_threadsafe with a synchronous runner so connect/disconnect run
    without a live event loop (ignore the loop arg). Returns a context-manager patch."""
    def _fake(coro, loop):
        fut = concurrent.futures.Future()
        try:
            fut.set_result(asyncio.run(coro))
        except BaseException as e:  # noqa: BLE001 — mirror what the loop future would carry
            fut.set_exception(e)
        return fut
    return patch.object(asyncio, 'run_coroutine_threadsafe', _fake)


def _no_cold_interrogate():
    return patch('agent_cascade.settings.MCP_LAZY_COLD_INTERROGATE', False)


def _wait_until(fn, timeout=5.0, interval=0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(interval)
    return False


class TestLazyBoot:
    def test_boot_connects_zero(self):
        """directive #1: park enabled servers, off disabled ones, connect NOTHING, no MCPClient."""
        mgr = _make_mgr()
        templates = [_FakeTpl()]
        config = {
            'srv-on': {'enabled': True, 'command': 'cmd'},
            'srv-off': {'enabled': False, 'command': 'cmd'},
        }
        with _no_cold_interrogate(), \
             patch('agent_cascade.tools.mcp_manager.MCPClient') as McpClient:
            result = mgr.init_lazy(config, templates)
        assert mgr._connected_servers == {}
        assert not McpClient.called
        assert 'srv-on' in mgr._parked_servers
        assert 'srv-off' not in mgr._parked_servers
        assert 'srv-off' in mgr._config_disabled and 'srv-off' in mgr._suppressed
        assert result['registered'] == 0  # cold cache + no interrogation → no stubs

    def test_stub_overwritten_on_connect(self):
        """H/E: a lazy stub in function_map MUST be overwritten by the live tool on connect."""
        mgr = _make_mgr()
        tpl = _FakeTpl()
        mgr._parked_servers['chrome-devtools'] = {'command': 'cmd'}
        mgr.register_stubs('chrome-devtools', {'command': 'cmd'},
                           [{'name': 'chrome-devtools-click', 'description': 'stub',
                             'parameters': {'type': 'object', 'properties': {}, 'required': []}}],
                           [tpl])
        stub = tpl.function_map['chrome-devtools-click']
        assert getattr(stub, '_mcp_stub', False) is True
        live = _live_tool('chrome-devtools-click')
        with _patch_rcts(), \
             patch('agent_cascade.tools.mcp_manager.MCPClient') as McpClient:
            McpClient.return_value.connection_server = AsyncMock(return_value=None)
            McpClient.return_value.cleanup = AsyncMock(return_value=None)
            mgr._build_tools_for_client = AsyncMock(return_value=[live])
            ok = mgr.connect_server('chrome-devtools', {'command': 'cmd'}, [tpl])
        assert ok is True
        registered = tpl.function_map['chrome-devtools-click']
        assert registered is live                      # overwritten, not left as stub
        assert getattr(registered, '_mcp_stub', False) is False
        assert mgr._tool_server_map['chrome-devtools-click'] == 'chrome-devtools'


class TestOnUseConnect:
    def _recording_connect(self, mgr, calls):
        """Fake connect_server that records into _connected_servers (so double-use sees 'connected')."""
        def _fake(server_name, server_config, templates, timeout_s=None):
            calls.append({'server': server_name, 'timeout_s': timeout_s})
            mgr._connected_servers[f'{server_name}:deadbeef'] = server_name + '_cid'
            return True
        return _fake

    def test_first_use_connects(self):
        mgr = _make_mgr()
        pool = _FakePool({'assistant': _FakeTpl()})
        mgr._parked_servers['srv'] = {'command': 'cmd'}
        calls = []
        with patch.object(mgr, 'connect_server', side_effect=self._recording_connect(mgr, calls)):
            assert mgr.ensure_connected('srv', pool=pool) is True
        assert len(calls) == 1 and calls[0]['server'] == 'srv'

    def test_double_use_no_respawn(self):
        mgr = _make_mgr()
        pool = _FakePool({'assistant': _FakeTpl()})
        mgr._parked_servers['srv'] = {'command': 'cmd'}
        calls = []
        with patch.object(mgr, 'connect_server', side_effect=self._recording_connect(mgr, calls)):
            assert mgr.ensure_connected('srv', pool=pool) is True
            assert mgr.ensure_connected('srv', pool=pool) is True
        assert len(calls) == 1  # second call sees it connected, never re-connects

    def test_connect_timeout_bounded(self):
        """H2/DMS: ensure_connected passes the per-step timeout, and the DMS bound is ≥ the
        real connect worst-case (2×timeout) so the holder is never killed mid-tail."""
        from agent_cascade import settings
        from agent_cascade.tools.mcp_manager import lazy_connect_dms_bound
        mgr = _make_mgr()
        pool = _FakePool({'assistant': _FakeTpl()})
        mgr._parked_servers['srv'] = {'command': 'cmd'}
        calls = []
        with patch.object(mgr, 'connect_server', side_effect=self._recording_connect(mgr, calls)):
            mgr.ensure_connected('srv', pool=pool)
        step = settings.MCP_LAZY_CONNECT_TIMEOUT_S
        assert calls[0]['timeout_s'] == step  # both connect + tools/list are bounded by this
        # DMS bound must cover BOTH bounded steps sequentially (+ teardown) — never shorter.
        assert lazy_connect_dms_bound() >= 2 * step

    def test_disabled_server_never_connects(self):
        """A config enabled:false server is suppressed; ensure_connected refuses, no connect."""
        mgr = _make_mgr()
        pool = _FakePool({'assistant': _FakeTpl()})
        with _no_cold_interrogate():
            mgr.init_lazy({'off': {'enabled': False, 'command': 'cmd'}}, [_FakeTpl()])
        with patch.object(mgr, 'connect_server') as connect:
            assert mgr.ensure_connected('off', pool=pool) is False
            assert not connect.called


class TestAllDisabledOff:
    def test_all_disabled_off(self):
        mgr = _make_mgr()
        mgr._parked_servers['srv'] = {'command': 'cmd'}
        mgr._tool_server_map = {'srv-click': 'srv', 'srv-snap': 'srv'}
        pool = _FakePool(
            {'assistant': _FakeTpl(), 'coder': _FakeTpl()},
            disabled_map={'assistant': {'srv-click', 'srv-snap'},
                          'coder': {'srv-click', 'srv-snap'}},
        )
        mgr.apply_ui_disabled(pool)
        assert 'srv' in mgr._suppressed
        assert mgr._connected_servers == {}  # parked, nothing to disconnect

    def test_all_mcp_sentinel_off(self):
        """The __all_mcp_tools__ sentinel for every agent also turns the server off."""
        mgr = _make_mgr()
        mgr._parked_servers['srv'] = {'command': 'cmd'}
        mgr._tool_server_map = {'srv-click': 'srv'}
        pool = _FakePool(
            {'assistant': _FakeTpl(), 'coder': _FakeTpl()},
            disabled_map={'assistant': {'__all_mcp_tools__'}, 'coder': {'__all_mcp_tools__'}},
        )
        mgr.apply_ui_disabled(pool)
        assert 'srv' in mgr._suppressed

    def test_re_enable_restores(self):
        """A suppressed server that is no longer fully disabled returns to parked (stub restored)."""
        mgr = _make_mgr()
        mgr._parked_servers['srv'] = {'command': 'cmd'}
        mgr._suppressed = {'srv'}
        mgr._tool_server_map = {'srv-click': 'srv'}  # names known, so re-enable is meaningful
        tpl = _FakeTpl()
        cache = {'srv': {'config_hash': 'aabbccdd',
                         'tools': [{'name': 'srv-click', 'description': 'x',
                                    'parameters': {'type': 'object', 'properties': {}, 'required': []}}]}}
        pool = _FakePool({'assistant': tpl}, disabled_map={'assistant': set()})  # nothing disabled now
        with patch.object(mgr, '_load_tools_cache', return_value=cache):
            mgr.apply_ui_disabled(pool)
        assert 'srv' not in mgr._suppressed
        assert 'srv-click' in tpl.function_map
        assert getattr(tpl.function_map['srv-click'], '_mcp_stub', False) is True


class TestColdCacheBackground:
    def test_cold_cache_background(self):
        """Cold cache + cold-interrogation ON: a background thread populates the cache and
        registers stubs. Poll (never assert immediately); nothing is connected at boot instant."""
        tmp_cache = Path(__file__).parent / '_bug0310_lazy_cache_test.json'
        if tmp_cache.exists():
            tmp_cache.unlink()
        mgr = _make_mgr()
        tpl = _FakeTpl()
        live = _live_tool('a-tool')
        with patch.object(mcp_manager, 'MCP_TOOLS_CACHE_PATH', tmp_cache), \
             _patch_rcts(), \
             patch('agent_cascade.tools.mcp_manager.MCPClient') as McpClient:
            McpClient.return_value.connection_server = AsyncMock(return_value=None)
            McpClient.return_value.cleanup = AsyncMock(return_value=None)
            mgr._build_tools_for_client = AsyncMock(return_value=[live])
            # boot: connects zero synchronously (interrogation is backgrounded)
            mgr.init_lazy({'a': {'enabled': True, 'command': 'cmd'}}, [tpl])
            assert mgr._connected_servers == {}  # at the instant of boot, nothing connected
            # poll for the background thread to register the stub
            got = _wait_until(lambda: 'a-tool' in tpl.function_map, timeout=8.0)
        try:
            assert got, 'background cold-cache thread never registered the stub'
            assert 'a-tool' in tpl.function_map
            data = json.loads(tmp_cache.read_text(encoding='utf-8'))
            assert 'a' in data and data['a'].get('tools')
            # interrogate-then-park: the server must NOT be left connected
            assert mgr._connected_servers == {}
        finally:
            if tmp_cache.exists():
                tmp_cache.unlink()


class TestKnobOffEager:
    def test_knob_off_eager(self):
        """MCP_LAZY_CONNECT=0 routes shared_init back to the eager mcpServers handler and never
        calls init_lazy — the kill-switch restores the exact old connect-at-boot path."""
        from agent_cascade import shared_init

        tmp_root = Path(__file__).parent / '_bug0310_eager_root'
        (tmp_root / 'config').mkdir(parents=True, exist_ok=True)
        (tmp_root / 'config' / 'mcp_servers.json').write_text(
            json.dumps({'srv': {'enabled': True, 'command': 'cmd'}}), encoding='utf-8')
        try:
            handler = MagicMock(name='mcpServersHandler')
            with patch('agent_cascade.settings.MCP_LAZY_CONNECT', False), \
                 patch('agent_cascade.config_handlers.CONFIG_HANDLERS', {'mcpServers': handler}), \
                 patch('agent_cascade.tools.mcp_manager.MCPManager') as McpMgrCls:
                McpMgrCls.return_value._connected_servers = {}
                pool = MagicMock()
                pool.templates = {}
                shared_init.initialize_mcp_servers(pool, str(tmp_root))
            assert handler.called, 'eager path must use the mcpServers handler'
            McpMgrCls.return_value.init_lazy.assert_not_called()
        finally:
            (tmp_root / 'config' / 'mcp_servers.json').unlink(missing_ok=True)
            (tmp_root / 'config').rmdir()
            tmp_root.rmdir()
