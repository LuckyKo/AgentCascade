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

    def test_all_disabled_live_server_disconnects(self):
        """apply_ui_disabled must LIVE-disconnect a CONNECTED server whose tools are all
        disabled (mcp_manager.py apply_ui_disabled: ``if self._is_connected(server):
        disconnect_server(...)``). test_all_disabled_off only covers a parked server (no
        disconnect); this reaches that live branch. The real disconnect_server runs (so the
        server is genuinely removed from _connected_servers) while a spy records the call."""
        mgr = _make_mgr()
        # Live, not parked: present in _connected_servers with a known tool set so
        # _server_fully_disabled sees a non-empty tool set for 'srv'.
        mgr._connected_servers['srv:deadbeef'] = 'srv_cid'
        mgr._tool_server_map = {'srv-click': 'srv', 'srv-snap': 'srv'}
        mgr._server_tools['srv'] = [_live_tool('srv-click'), _live_tool('srv-snap')]
        pool = _FakePool(
            {'assistant': _FakeTpl(), 'coder': _FakeTpl()},
            disabled_map={'assistant': {'srv-click', 'srv-snap'},
                          'coder': {'srv-click', 'srv-snap'}},
        )
        real_disconnect = mgr.disconnect_server
        with patch.object(mgr, 'disconnect_server',
                          side_effect=lambda s, t: real_disconnect(s, t)) as spy:
            mgr.apply_ui_disabled(pool)
        spy.assert_called_once()
        assert spy.call_args.args[0] == 'srv'
        assert 'srv' in mgr._suppressed
        assert mgr._connected_servers == {}  # disconnect_server actually cleared the live entry


class TestConcurrentSingleFlight:
    def test_concurrent_first_use_connects_once(self):
        """N threads race ensure_connected('srv') on one parked server. A barrier-gated fake
        connect_server makes every thread hit the guard at the same instant, so the WAITER
        branch (``ev.wait``) actually executes rather than every thread silently re-connecting.

        The point of the test: the owner's ``finally`` (mcp_manager.ensure_connected) pops
        ``_connecting[server]`` AND calls ``claim.set()`` on BOTH success and failure. If that
        ``claim.set()`` were broken/removed, the owner still returns True (it captured ``ok``
        before the finally) and still records the connection, so 'exactly once' + 'all True'
        would stay green — but every waiter would block the full ``ev.wait(2*timeout_s + margin)``
        (~7s here) instead of returning the instant the owner finishes (~0.1s). That divergence
        is what the wall-clock bound below turns into a failing assertion, so this test genuinely
        fails if the finally-claim.set() is broken.
        """
        n = 8
        mgr = _make_mgr()
        pool = _FakePool({'assistant': _FakeTpl()})
        mgr._parked_servers['srv'] = {'command': 'cmd'}
        calls: list[str] = []
        calls_lock = threading.Lock()

        def _fake_connect(server_name, server_config, templates, timeout_s=None):
            with calls_lock:
                calls.append(server_name)
            # Hold the _connecting guard open so the other n-1 threads reliably observe it
            # set and enter ev.wait() (the waiter path) before the owner records a connection.
            time.sleep(0.1)
            mgr._connected_servers[f'{server_name}:deadbeef'] = server_name + '_cid'
            return True

        results = [None] * n
        barrier = threading.Barrier(n)

        def _worker(i):
            barrier.wait(timeout=5)
            results[i] = mgr.ensure_connected('srv', pool=pool, timeout_s=1.0)

        # The patch must be live while the threads run: all n workers share mgr, so
        # self.connect_server resolves to _fake_connect inside every thread. Without this
        # wrap, ensure_connected calls the REAL connect_server (which errors on loop=None and
        # returns fast), so multiple threads become owners — not the single-flight path.
        with patch.object(mgr, 'connect_server', side_effect=_fake_connect):
            threads = [threading.Thread(target=_worker, args=(i,), name=f'ensure-srv-{i}')
                       for i in range(n)]
            start = time.perf_counter()
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)
            elapsed = time.perf_counter() - start

        # single-flight: exactly one connect despite n racing first-use callers
        assert len(calls) == 1, f'connect_server called {len(calls)} times, expected exactly 1'
        # every caller sees success: the owner connects, waiters blocked on the Event and re-checked
        assert results == [True] * n, results
        # waiters were RELEASED by claim.set() — not left to hit their ~7s timeouts
        assert elapsed < 3.0, (
            f'waiters not released promptly (elapsed {elapsed:.2f}s); a broken owner '
            'finally-claim.set() would block ~7s each')
        assert mgr._connecting == {}  # guard cleaned up: no stale Event, no leaked waiter
        assert not any(t.is_alive() for t in threads)


class TestLazyConnectBounds:
    def test_waiter_bounded_when_owner_hangs(self):
        """A waiter's ev.wait is time-bounded: even if the owner's connect_server hangs
        forever, a concurrent ensure_connected does NOT wedge — it returns within its own
        bound (2*timeout_s + teardown margin, twice) and degrades to False. This is the lazy
        path's runtime wall-clock guarantee (the analogue of connect_server's .result(timeout=)):
        a hung owner can never wedge every first-use caller of that server.
        """
        mgr = _make_mgr()
        pool = _FakePool({'assistant': _FakeTpl()})
        mgr._parked_servers['srv'] = {'command': 'cmd'}
        entered = threading.Event()
        release = threading.Event()

        def _hanging_connect(server_name, server_config, templates, timeout_s=None):
            entered.set()
            release.wait(timeout=20)  # owner parks in connect_server → claim never set
            return False

        timeout_s = 0.05
        # Zero the teardown margin so the bounded waits are fast without changing the bound logic.
        with patch.object(mgr, 'connect_server', side_effect=_hanging_connect), \
             patch('agent_cascade.tools.mcp_manager._MCP_CONNECT_TEARDOWN_MARGIN_S', 0.0):
            owner = threading.Thread(
                target=lambda: mgr.ensure_connected('srv', pool=pool, timeout_s=timeout_s),
                name='owner')
            owner.start()
            assert entered.wait(timeout=5), 'owner never entered connect_server'
            start = time.perf_counter()
            got = mgr.ensure_connected('srv', pool=pool, timeout_s=timeout_s)  # this thread is the waiter
            elapsed = time.perf_counter() - start
            release.set()
            owner.join(timeout=5)

        # 2 attempts × (2*timeout_s + margin=0) + slack — far below the 20s hang the owner held.
        bound = 2 * (2 * timeout_s) + 1.0
        assert got is False, f'waiter should degrade to False when owner hangs, got {got!r}'
        assert elapsed < bound, f'waiter not bounded: {elapsed:.3f}s >= {bound}s'


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
