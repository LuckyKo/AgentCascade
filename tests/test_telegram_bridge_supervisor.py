"""Hermetic unit tests for the Telegram bridge supervisor (in-process daemon thread).

No live Telegram, no real PTB Application, no live AC. The thread body builds the
app through a module-level ``_build_app`` seam; tests patch it with a fake app
whose ``run_polling(...)`` records kwargs and blocks on an Event until stop is
driven (or raises/returns per test). This keeps everything deterministic — no
fixed sleep-settle windows, no subprocess.

Run serially (pytest.ini pins xdist in addopts):
    python -m pytest tests/test_telegram_bridge_supervisor.py -o addopts="" --timeout=90
"""

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure top-level imports work (mirror tests/test_telegram_bridge.py convention).
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.agent_instance import PoolSettings  # noqa: E402
from agent_cascade.config_handlers import (  # noqa: E402
    CONFIG_HANDLERS,
    POOL_SETTINGS_KEYS,
    _handle_telegram_bridge_enabled,
)
import agent_cascade.telegram_bridge.supervisor as supervisor_mod  # noqa: E402
from agent_cascade.telegram_bridge.supervisor import TelegramBridgeSupervisor  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes + fixtures
# ---------------------------------------------------------------------------

class FakeApp:
    """Stand-in for a PTB Application that models the real loop lifecycle.

    ``run_polling(**kwargs)`` records kwargs, tracks concurrency (exactly one
    bridge thread may hold the bot token at a time), then runs a REAL asyncio
    loop in the calling (bridge) thread — mirroring PTB's ``run_forever()``:

      * a watcher task waits on an ``asyncio.Event``;
      * ``stop()`` is the coroutine the supervisor schedules onto that loop via
        ``run_coroutine_threadsafe(app.stop(), loop)`` — awaiting it sets the
        event, the watcher returns, and run_polling tears down (loop closed).

    This makes the stop path behave exactly like PTB: if the supervisor calls
    ``stop_running()`` directly (RuntimeError off-loop) or skips the join, the
    tests fail. Behavior knobs:
      - ``raise_exc``: if set, run_polling raises it before starting the loop.
      - ``clean_return``: if True, run_polling returns immediately (no loop).
      - ``stop_delay_sec``: extra sleep inside stop() before signaling (slow-stop test).
    """

    _concurrency = 0
    _max_concurrency = 0
    _lock = threading.Lock()

    def __init__(self, base_url=None, raise_exc=None, clean_return=False, stop_delay_sec=0.0):
        self.base_url = base_url
        self.raise_exc = raise_exc
        self.clean_return = clean_return
        self.stop_delay_sec = stop_delay_sec
        self.run_kwargs = None
        self.run_calls = 0
        self._loop = None            # the "bridge loop" (real, per run)
        self._stop_event = None      # asyncio.Event on that loop
        self.post_init = None        # coroutine fn; supervisor assigns it (PTB-style)

    @classmethod
    def reset_concurrency(cls):
        with cls._lock:
            cls._concurrency = 0
            cls._max_concurrency = 0

    async def stop(self):
        """The coroutine the supervisor schedules onto the bridge loop."""
        if self.stop_delay_sec > 0:
            await asyncio.sleep(self.stop_delay_sec)
        if self._stop_event is not None:
            self._stop_event.set()

    def run_polling(self, **kwargs):
        self.run_kwargs = kwargs
        self.run_calls += 1
        with FakeApp._lock:
            FakeApp._concurrency += 1
            FakeApp._max_concurrency = max(FakeApp._max_concurrency, FakeApp._concurrency)
        try:
            if self.raise_exc is not None:
                raise self.raise_exc
            if self.clean_return:
                return
            # Model PTB: own a real loop in this (bridge) thread. The supervisor
            # publishes it via the post_init hook; app.stop() is scheduled onto it.
            import asyncio as _aio

            async def _run():
                self._stop_event = _aio.Event()
                # PTB runs post_init (assigned by the supervisor) right after init,
                # inside the loop — this is what publishes the loop to the supervisor.
                hook = getattr(self, 'post_init', None)
                if hook is not None:
                    await hook(self)
                await self._stop_event.wait()

            loop = _aio.new_event_loop()
            self._loop = loop
            try:
                loop.run_until_complete(_run())
            finally:
                self._loop = None
                self._stop_event = None
                loop.close()
        finally:
            with FakeApp._lock:
                FakeApp._concurrency -= 1


def make_supervisor(tmp_path, **kw):
    """Build a supervisor with fast timers (no real sleeping in tests)."""
    defaults = dict(
        ac_base_url='http://127.0.0.1:8126',
        project_root=PROJECT_ROOT,
        workspace_dir=str(tmp_path),
        backoff_base=0.01,         # tiny sleep between restarts
        backoff_cap=0.02,
        max_restart_attempts=3,
        stop_join_timeout_sec=5.0,
    )
    defaults.update(kw)
    return TelegramBridgeSupervisor(**defaults)


def _wait_until(cond, timeout=6.0):
    """Poll cond() until true or timeout (for thread-driven assertions)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


@pytest.fixture
def fake_app_patched(tmp_path):
    """Patch supervisor_mod._build_app so no real PTB Application is constructed.

    Yields a dict with the queue of FakeApp instances and helpers. Each bridge
    attempt pops the next queued app (or reuses the last one if the queue is
    empty — useful for restart loops that rebuild on every attempt).
    """
    fakes = []
    built = []

    def _side_effect(cfg, ac):
        app = fakes.pop(0) if fakes else (built[-1] if built else FakeApp())
        built.append(app)
        return app

    with patch.object(supervisor_mod, '_build_app', side_effect=_side_effect):
        FakeApp.reset_concurrency()
        yield {
            'queue': lambda a: fakes.append(a),
            'built': built,
            'max_concurrency': lambda: FakeApp._max_concurrency,
        }


def _bridge_threads():
    return [t for t in threading.enumerate() if t.name == 'tg-bridge' and t.is_alive()]


@pytest.fixture(autouse=True)
def _reap_stale_bridge_threads():
    """Safety net: unblock + join any tg-bridge thread leaked by a previous test.

    A stop() that hits its join timeout leaves the (daemon) thread alive; if it
    is still blocked in run_polling when the NEXT test starts, that test's fresh
    supervisor would see two concurrent bridge threads. Reaping here keeps each
    test hermetic. In a healthy implementation no thread ever survives stop().

    Strategy: for each leaked thread, find its owner supervisor and try to drive
    app.stop() onto the bridge loop. If the supervisor already cleared _app/_loop
    (normal stop path), the thread should be exiting on its own — just wait.
    """
    yield
    # Best-effort teardown: a leaked-thread cleanup failure must NOT fail the test,
    # so we swallow errors here deliberately. In a healthy implementation no thread
    # ever survives stop(), so this path is only hit if a test left a stuck daemon
    # thread behind — unblocking it keeps the NEXT test hermetic.
    for t in _bridge_threads():
        try:
            sup = getattr(t, '_owner_supervisor', None)
            if sup is not None:
                with sup._lock:
                    app, loop = sup._app, sup._loop
                if app is not None and loop is not None and not loop.is_closed():
                    import asyncio as _aio
                    fut = _aio.run_coroutine_threadsafe(app.stop(), loop)
                    fut.result(timeout=1.0)  # may raise on timeout — fine, thread is daemon
        except Exception:
            # Intentional: teardown must never fail the test (see note above).
            continue
    # Wait for all leaked threads to finish (they should exit shortly after unblock).
    deadline = time.monotonic() + 3.0
    while _bridge_threads() and time.monotonic() < deadline:
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# A. Start / thread lifecycle
# ---------------------------------------------------------------------------

def test_start_spawns_one_daemon_thread_idempotent(tmp_path, fake_app_patched):
    """set_enabled(True) spawns exactly one daemon thread named 'tg-bridge';
    a second set_enabled(True) is a no-op (idempotency)."""
    app = FakeApp(base_url='http://127.0.0.1:8126')
    fake_app_patched['queue'](app)
    sup = make_supervisor(tmp_path)
    try:
        sup.set_enabled(True)
        assert _wait_until(sup.is_running), 'bridge thread should be running'
        threads = _bridge_threads()
        assert len(threads) == 1, f'expected exactly one tg-bridge thread, got {len(threads)}'
        assert threads[0].daemon is True
        # Wait for the app to be built (happens in the bridge thread).
        assert _wait_until(lambda: fake_app_patched['built']), 'app should be built'
        assert fake_app_patched['built'][-1] is app, 'the queued app should be the one built'
        assert app.base_url == 'http://127.0.0.1:8126'

        sup.set_enabled(True)   # UI re-sends full snapshot -> must not double-start
        time.sleep(0.1)
        assert len(_bridge_threads()) == 1, 'second set_enabled(True) must not spawn a second thread'
    finally:
        sup.stop()


def test_stop_drives_ptb_stop_and_joins_cleanly(tmp_path, fake_app_patched):
    """KEY REGRESSION: stop() schedules app.stop() onto the bridge loop (NOT
    app.stop_running(), which raises off-loop) and joins cleanly."""
    app = FakeApp()
    fake_app_patched['queue'](app)
    sup = make_supervisor(tmp_path, stop_join_timeout_sec=5.0)
    sup.set_enabled(True)
    assert _wait_until(sup.is_running), 'bridge thread should be running'
    # run_polling must have been called with the plan's exact kwargs.
    assert _wait_until(lambda: app.run_kwargs is not None), 'run_polling should be called'
    assert app.run_kwargs == {
        'allowed_updates': ['message'],
        'drop_pending_updates': False,
        'stop_signals': None,
    }

    t0 = time.monotonic()
    sup.stop()
    elapsed = time.monotonic() - t0

    # stop() returned => the thread was joined (not abandoned). Clean join is fast.
    assert elapsed < 5.0, f'stop() should join within timeout, took {elapsed:.2f}s'
    assert sup.is_running() is False
    assert _wait_until(lambda: len(_bridge_threads()) == 0), 'thread should be gone after stop'

    # set_enabled(False) again is a safe no-op (nothing running).
    sup.set_enabled(False)
    assert sup.is_running() is False


def test_stop_when_never_started_is_safe_noop(tmp_path, fake_app_patched):
    sup = make_supervisor(tmp_path)
    sup.stop()   # nothing running — must not raise
    assert sup.is_running() is False
    assert sup.status()['enabled'] is False


# ---------------------------------------------------------------------------
# B. Crash containment / restart policy
# ---------------------------------------------------------------------------

def test_crash_contained_bounded_restart_then_give_up(tmp_path, fake_app_patched):
    """A RuntimeError from run_polling never escapes the thread; with
    max_restart_attempts=2 (TOTAL attempts) the bridge runs exactly 2 times
    then stops with an error. The main test thread is never interrupted."""
    sup = make_supervisor(tmp_path, backoff_base=0.01, backoff_cap=0.02, max_restart_attempts=2)
    # Every attempt gets a fresh crashing app (queue empty -> reuse last built).
    fake_app_patched['queue'](FakeApp(raise_exc=RuntimeError('boom')))

    sup.set_enabled(True)
    assert _wait_until(lambda: not sup.is_running() and sup.status()['error'], timeout=10), \
        'bridge should have given up after max_restart_attempts'
    time.sleep(0.2)   # settle: make sure no late restart sneaks in

    # Count attempts by how many times _build_app was called (each attempt builds fresh).
    # Note: the fixture reuses built[-1] when the queue is empty, so run_calls on a
    # single object accumulates; len(built) is the reliable attempt count.
    total_attempts = len(fake_app_patched['built'])
    assert total_attempts == 2, f'expected exactly 2 attempts (1 initial + 1 retry), got {total_attempts}'
    assert 'giving up' in sup.status()['error'].lower()
    # The main test thread was never interrupted — we're still here.
    assert True


def test_config_failure_is_permanent_stop(tmp_path, fake_app_patched):
    """validate_config problems -> permanent stop, no retry; re-enabling after
    the "fix" starts again."""
    sup = make_supervisor(tmp_path)

    # The thread body imports these from .config, so patch them at the source.
    with patch('agent_cascade.telegram_bridge.config.load_config') as mock_load, \
         patch('agent_cascade.telegram_bridge.config.validate_config',
               return_value=['ALLOWED_USERS is empty']):
        mock_load.return_value = MagicMock(bot_token='x', allowed_users=[])
        sup.set_enabled(True)
        assert _wait_until(lambda: not sup.is_running() and sup.status()['error'], timeout=10)
        time.sleep(0.2)
        # No app was ever built (config failed before _build_app).
        assert fake_app_patched['built'] == [], 'config failure must happen before app build'
        err = sup.status()['error'].lower()
        assert 'allowlist' in err or 'allowed_users' in err

    # "Fix" the config: now validate passes and a real (fake) app runs.
    # Must explicitly stop first to reset _enabled=False, since the config-failure
    # path leaves _enabled=True (the toggle is still on; the bridge just can't start).
    sup.stop()
    good_app = FakeApp(base_url='http://127.0.0.1:8126')
    fake_app_patched['queue'](good_app)
    sup.set_enabled(True)
    try:
        assert _wait_until(sup.is_running, timeout=10), 're-enable after fix should start the bridge'
    finally:
        sup.stop()


def test_clean_return_no_restart(tmp_path, fake_app_patched):
    """A clean return from run_polling means "someone stopped it" -> no restart,
    even though _enabled is still True. Re-enabling starts a fresh run."""
    app = FakeApp(clean_return=True)
    fake_app_patched['queue'](app)
    sup = make_supervisor(tmp_path)

    sup.set_enabled(True)
    assert _wait_until(lambda: not sup.is_running(), timeout=10), 'clean return should end the thread'
    time.sleep(0.2)
    assert app.run_calls == 1, 'clean return must NOT trigger a restart'
    assert sup.status()['error'] == '', 'a clean stop is not an error'

    # Re-enable -> fresh run with a new (blocking) app.
    # Must explicitly stop first to reset _enabled=False (clean return leaves it True).
    sup.stop()
    app2 = FakeApp(base_url='http://127.0.0.1:8126')
    fake_app_patched['queue'](app2)
    sup.set_enabled(True)
    try:
        assert _wait_until(sup.is_running, timeout=10), 're-enable after clean return should start fresh'
    finally:
        sup.stop()


def test_start_while_previous_still_shutting_down_no_double_start(tmp_path, fake_app_patched):
    """stop() then immediately start(): the new thread must wait for the old one
    to finish — max concurrency of bridge threads is 1."""
    slow_app = FakeApp(stop_delay_sec=0.3)   # PTB stop takes a bit
    fake_app_patched['queue'](slow_app)
    sup = make_supervisor(tmp_path, stop_join_timeout_sec=5.0)

    sup.set_enabled(True)
    assert _wait_until(sup.is_running), 'bridge thread should be running'

    # Drive stop (schedules app.stop(); join waits for the 0.3s teardown).
    sup.stop()
    # Immediately start again — must wait out the old thread, not double-start.
    fresh_app = FakeApp(base_url='http://127.0.0.1:8126')
    fake_app_patched['queue'](fresh_app)
    sup.start()
    assert _wait_until(sup.is_running, timeout=10), 'new bridge should be running'

    assert fake_app_patched['max_concurrency']() <= 1, \
        f"never more than one concurrent bridge thread (got {fake_app_patched['max_concurrency']()})"
    sup.stop()


# ---------------------------------------------------------------------------
# C. Config handler (contract preserved — same calls as before)
# ---------------------------------------------------------------------------

def test_handler_registered_and_in_persist_keys():
    assert 'telegram_bridge_enabled' in POOL_SETTINGS_KEYS
    assert 'telegram_bridge_enabled' in CONFIG_HANDLERS
    assert CONFIG_HANDLERS['telegram_bridge_enabled'] is _handle_telegram_bridge_enabled


def test_handler_sets_field_and_drives_supervisor_on():
    pool = MagicMock()
    pool.settings = PoolSettings()
    sup = MagicMock()
    pool.telegram_supervisor = sup

    _handle_telegram_bridge_enabled({'telegram_bridge_enabled': True}, pool, [])

    assert pool.settings.telegram_bridge_enabled is True
    sup.set_enabled.assert_called_once_with(True)


def test_handler_sets_field_and_drives_supervisor_off():
    pool = MagicMock()
    pool.settings = PoolSettings(telegram_bridge_enabled=True)
    sup = MagicMock()
    pool.telegram_supervisor = sup

    _handle_telegram_bridge_enabled({'telegram_bridge_enabled': False}, pool, [])

    assert pool.settings.telegram_bridge_enabled is False
    sup.set_enabled.assert_called_once_with(False)


def test_handler_idempotent_on_repeated_full_snapshot():
    """UI sends a full snapshot on every save — handler must be safe to re-run."""
    pool = MagicMock()
    pool.settings = PoolSettings()
    sup = MagicMock()
    pool.telegram_supervisor = sup

    _handle_telegram_bridge_enabled({'telegram_bridge_enabled': True}, pool, [])
    _handle_telegram_bridge_enabled({'telegram_bridge_enabled': True}, pool, [])  # second save

    assert pool.settings.telegram_bridge_enabled is True
    # set_enabled called twice (once per snapshot) but the SUPERVISOR itself is
    # idempotent — verified separately in test_start_spawns_one_daemon_thread_idempotent.
    assert sup.set_enabled.call_count == 2


def test_handler_noop_when_pool_none():
    _handle_telegram_bridge_enabled({'telegram_bridge_enabled': True}, None, [])   # must not raise


def test_handler_no_supervisor_attached_is_safe():
    pool = MagicMock(spec=[])   # no attributes at all -> hasattr checks fail safely
    pool.settings = PoolSettings()
    _handle_telegram_bridge_enabled({'telegram_bridge_enabled': True}, pool, [])   # must not raise
    assert pool.settings.telegram_bridge_enabled is True


# ---------------------------------------------------------------------------
# D. Persistence round-trip
# ---------------------------------------------------------------------------

def test_pool_settings_roundtrip():
    ps = PoolSettings(telegram_bridge_enabled=True)
    d = ps.to_dict()
    assert d['telegram_bridge_enabled'] is True
    restored = PoolSettings.from_dict({'telegram_bridge_enabled': True})
    assert restored.telegram_bridge_enabled is True
    # Absent key -> default False.
    assert PoolSettings.from_dict({}).telegram_bridge_enabled is False


# ---------------------------------------------------------------------------
# E. State serialization (both blocks must carry the field)
# ---------------------------------------------------------------------------

def test_state_builder_serializes_telegram_bridge_in_both_blocks():
    """Guard against the 'miss one of two blocks' gotcha: both dict literals in
    state_builder.py must expose telegram_bridge_enabled."""
    import inspect
    from agent_cascade.api_integration_pkg import state_builder as sb
    src = inspect.getsource(sb)
    count = src.count("'telegram_bridge_enabled'")
    assert count >= 2, f"expected telegram_bridge_enabled in both serialization blocks, found {count}"


# ---------------------------------------------------------------------------
# F. Base URL lazy resolution (_resolve_base_url) — unchanged contract
# ---------------------------------------------------------------------------
# The create_app() attach path constructs the supervisor with an EMPTY
# ac_base_url (uvicorn has not bound yet), so the base URL is resolved lazily
# at start time from agent_pool.server_info, falling back to AGENT_CASCADE_PORT
# env, then default 8765. The host is ALWAYS forced to 127.0.0.1 — a launcher
# may bind to 0.0.0.0 for LAN access, but 0.0.0.0 is a bind address, not a
# valid connect target for the (always-local) bridge client. _resolve_base_url()
# is pure (no thread spawn), so these tests call it directly.

class FakePool:
    """Minimal agent_pool stand-in exposing only server_info."""
    def __init__(self, server_info):
        self.server_info = server_info


def test_resolve_base_url_explicit_wins(tmp_path):
    """An explicit ac_base_url always wins, even when server_info is set."""
    sup = make_supervisor(tmp_path, ac_base_url='http://127.0.0.1:8126',
                          agent_pool=FakePool(('0.0.0.0', 9999)))
    assert sup._resolve_base_url() == 'http://127.0.0.1:8126'


def test_resolve_base_url_from_server_info_tuple(tmp_path):
    """server_info tuple -> port taken, host forced to localhost."""
    sup = make_supervisor(tmp_path, ac_base_url='', agent_pool=FakePool(('127.0.0.1', 9999)))
    assert sup._resolve_base_url() == 'http://127.0.0.1:9999'


def test_resolve_base_url_forces_localhost_when_server_info_is_bind_all(tmp_path):
    """KEY REGRESSION: a 0.0.0.0 bind host must be forced to 127.0.0.1.

    start_multi_agent.py sets server_info = ('0.0.0.0', port) for LAN access;
    0.0.0.0 is not a valid connect target, so the client URL must use 127.0.0.1.
    """
    sup = make_supervisor(tmp_path, ac_base_url='', agent_pool=FakePool(('0.0.0.0', 8765)))
    assert sup._resolve_base_url() == 'http://127.0.0.1:8765'


def test_resolve_base_url_server_info_none_falls_back_to_env(tmp_path, monkeypatch):
    """server_info None -> AGENT_CASCADE_PORT env is used."""
    monkeypatch.setenv('AGENT_CASCADE_PORT', '7331')
    sup = make_supervisor(tmp_path, ac_base_url='', agent_pool=FakePool(None))
    assert sup._resolve_base_url() == 'http://127.0.0.1:7331'


def test_resolve_base_url_no_server_info_no_env_defaults(tmp_path, monkeypatch):
    """server_info None and env unset -> default 127.0.0.1:8765."""
    monkeypatch.delenv('AGENT_CASCADE_PORT', raising=False)
    sup = make_supervisor(tmp_path, ac_base_url='', agent_pool=FakePool(None))
    assert sup._resolve_base_url() == 'http://127.0.0.1:8765'


def test_resolve_base_url_agent_pool_none_defaults(tmp_path, monkeypatch):
    """agent_pool is None entirely -> default 127.0.0.1:8765, no raise."""
    monkeypatch.delenv('AGENT_CASCADE_PORT', raising=False)
    sup = make_supervisor(tmp_path, ac_base_url='', agent_pool=None)
    assert sup._resolve_base_url() == 'http://127.0.0.1:8765'


def test_resolve_base_url_malformed_server_info_port_defaults(tmp_path, monkeypatch):
    """server_info port not int-able -> default 8765, no raise."""
    monkeypatch.delenv('AGENT_CASCADE_PORT', raising=False)
    sup = make_supervisor(tmp_path, ac_base_url='', agent_pool=FakePool(('127.0.0.1', 'notaport')))
    assert sup._resolve_base_url() == 'http://127.0.0.1:8765'


def test_resolve_base_url_env_not_int_defaults(tmp_path, monkeypatch):
    """AGENT_CASCADE_PORT set but not int-able -> default 8765, no raise."""
    monkeypatch.setenv('AGENT_CASCADE_PORT', 'notaport')
    sup = make_supervisor(tmp_path, ac_base_url='', agent_pool=FakePool(None))
    assert sup._resolve_base_url() == 'http://127.0.0.1:8765'
