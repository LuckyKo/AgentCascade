"""Hermetic unit tests for the Telegram bridge supervisor (Phase 3).

No live Telegram, no real subprocess spawning a bot, no live AC. The child
process is a fake ``Popen``-like object so nothing OS-level is exercised.
``subprocess.Popen`` is patched for the ENTIRE duration of each test (via the
``popen_patched`` fixture) so the supervisor never spawns a real process — even
while the watcher thread runs in the background.

Run serially (pytest.ini pins xdist in addopts):
    python -m pytest tests/test_telegram_bridge_supervisor.py -o addopts="" --timeout=60
"""

import sys
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

class FakeProcess:
    """Minimal stand-in for subprocess.Popen that records lifecycle calls.

    Models the real Popen contract: once ``terminate()`` is called the process
    dies (``poll()`` returns non-None), mirroring a well-behaved child that
    honours SIGTERM/TerminateProcess. A test that needs a child which *ignores*
    terminate (to prove stop() escalates to kill()) subclasses and overrides
    ``terminate``/``wait`` — see StubbornProcess below.

    ``exit_code`` is what poll()/wait() report once set.
    """

    def __init__(self, exit_code=None):
        self.exit_code = exit_code
        self.pid = 424242
        self.terminated = False
        self.killed = False
        self.wait_calls = []

    @property
    def returncode(self):
        # Real Popen.returncode is None until reaped; after poll() it's the code.
        return self.exit_code if self.poll() is not None else None

    def poll(self):
        return self.exit_code

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self.exit_code

    def terminate(self):
        self.terminated = True
        # A well-behaved child dies on terminate: become "reaped" so stop() can
        # read its exit code. (StubbornProcess overrides this to stay alive.)
        if self.exit_code is None:
            self.exit_code = 143   # conventional SIGTERM/kill-on-Windows exit

    def kill(self):
        self.killed = True
        if self.exit_code is None:
            self.exit_code = -9


@pytest.fixture
def popen_patched():
    """Patch supervisor_mod.subprocess.Popen for the whole test.

    Yields a MagicMock whose return_value is replaced per-spawn via
    ``queue_fake(proc)`` (a FIFO of FakeProcess objects). Keeps the patch active
    while the watcher thread runs so no real process is ever spawned.
    """
    popen = MagicMock()
    fakes = []

    def _side_effect(*args, **kwargs):
        return fakes.pop(0) if fakes else FakeProcess()

    popen.side_effect = _side_effect

    with patch.object(supervisor_mod.subprocess, 'Popen', popen):
        yield {
            'popen': popen,
            'queue_fake': lambda p: fakes.append(p),
        }


def make_supervisor(tmp_path, **kw):
    """Build a supervisor with fast timers (no real sleeping in tests)."""
    defaults = dict(
        ac_base_url='http://127.0.0.1:8126',
        project_root=PROJECT_ROOT,
        workspace_dir=str(tmp_path),
        backoff_base=0.0,          # no sleep between restarts
        backoff_cap=0.0,
        max_restart_attempts=3,
        healthy_window_sec=0.0,
        stop_wait_sec=0.05,
        watch_interval_sec=0.01,
    )
    defaults.update(kw)
    return TelegramBridgeSupervisor(**defaults)


def _wait_until(cond, timeout=6.0):
    """Poll cond() until true or timeout (for watcher-thread-driven assertions)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


# ---------------------------------------------------------------------------
# A. Spawn args / env / CWD / CREATE_NO_WINDOW
# ---------------------------------------------------------------------------

def test_start_spawns_with_correct_args_env_cwd(tmp_path, popen_patched):
    fake = FakeProcess()
    popen_patched['queue_fake'](fake)
    sup = make_supervisor(tmp_path, allowed_users='123,456', target_agent='Maine')
    try:
        sup.start()
    finally:
        sup.stop()

    assert popen_patched['popen'].call_count == 1
    args, kwargs = popen_patched['popen'].call_args
    # Command: [sys.executable, '-m', 'agent_cascade.telegram_bridge']
    assert args[0] == [sys.executable, '-m', 'agent_cascade.telegram_bridge']
    # CWD = repo root (so config.secrets_loader resolves)
    assert kwargs['cwd'] == str(PROJECT_ROOT)
    # CREATE_NO_WINDOW on Windows (no console pop-up); 0 elsewhere.
    import subprocess as _sp
    expected_flags = getattr(_sp, 'CREATE_NO_WINDOW', 0)
    assert kwargs.get('creationflags', 0) == expected_flags
    if sys.platform == 'win32':
        assert kwargs['creationflags'] == 0x08000000

    # Env: non-secret vars set, token NOT passed.
    env = kwargs['env']
    assert env['TG_BRIDGE_ENABLED'] == 'true'
    assert env['ALLOWED_USERS'] == '123,456'
    assert env['AC_BASE_URL'] == 'http://127.0.0.1:8126'
    assert env['TG_TARGET_AGENT'] == 'Maine'
    # The bot token must never be in the child's environment.
    assert 'TELEGRAM_BOT_TOKEN' not in env


def test_start_env_omits_empty_allowed_users(tmp_path, popen_patched):
    """When allowed_users is empty, ALLOWED_USERS must NOT be in the child env.

    (target_agent defaults to 'Maine' so TG_TARGET_AGENT will be present — that's
    correct behavior, not a leak.)
    """
    fake = FakeProcess()
    popen_patched['queue_fake'](fake)
    sup = make_supervisor(tmp_path, allowed_users='', target_agent='Maine')
    try:
        sup.start()
    finally:
        sup.stop()
    env = popen_patched['popen'].call_args[1]['env']
    assert env['TG_BRIDGE_ENABLED'] == 'true'
    assert 'ALLOWED_USERS' not in env, 'empty allowed_users must not be passed to child'
    # TG_TARGET_AGENT is set (default 'Maine') — verify it's the right value.
    assert env.get('TG_TARGET_AGENT') == 'Maine'


def test_start_env_no_stale_host_vars_leak(tmp_path, popen_patched):
    """Stale bridge vars in os.environ must NOT leak into the child env."""
    import os
    # Simulate a host that has stale bridge vars set.
    old_tg = os.environ.get('TG_TARGET_AGENT')
    old_au = os.environ.get('ALLOWED_USERS')
    try:
        os.environ['TG_TARGET_AGENT'] = 'StaleAgent'
        os.environ['ALLOWED_USERS'] = '999,888'
        fake = FakeProcess()
        popen_patched['queue_fake'](fake)
        sup = make_supervisor(tmp_path, allowed_users='123', target_agent='Maine')
        try:
            sup.start()
        finally:
            sup.stop()
        env = popen_patched['popen'].call_args[1]['env']
        # The supervisor's values must win over the stale host values.
        assert env['TG_TARGET_AGENT'] == 'Maine', 'stale TG_TARGET_AGENT leaked'
        assert env['ALLOWED_USERS'] == '123', 'stale ALLOWED_USERS leaked'
    finally:
        # Restore original env.
        if old_tg is None:
            os.environ.pop('TG_TARGET_AGENT', None)
        else:
            os.environ['TG_TARGET_AGENT'] = old_tg
        if old_au is None:
            os.environ.pop('ALLOWED_USERS', None)
        else:
            os.environ['ALLOWED_USERS'] = old_au


def test_start_env_is_minimal_no_parent_secrets_leak(tmp_path, popen_patched):
    """The child env must NOT inherit the full parent environment.

    Regression guard for a critical security finding: _build_env() used to start
    from dict(os.environ), leaking any parent secrets (AWS creds, DB URLs, API
    keys) into the bridge process. It must now build a minimal allowlist instead.
    """
    import os
    sentinel_secret = 'FAKE_SECRET_VALUE_DO_NOT_LEAK'
    try:
        # Simulate sensitive vars present in the parent environment.
        os.environ['AWS_SECRET_ACCESS_KEY'] = sentinel_secret
        os.environ['DATABASE_URL'] = sentinel_secret
        os.environ['SOME_API_KEY'] = sentinel_secret

        fake = FakeProcess()
        popen_patched['queue_fake'](fake)
        sup = make_supervisor(tmp_path, allowed_users='123', target_agent='Maine')
        try:
            sup.start()
        finally:
            sup.stop()
        env = popen_patched['popen'].call_args[1]['env']

        # None of the parent's secrets may be present in the child env.
        for secret_key in ('AWS_SECRET_ACCESS_KEY', 'DATABASE_URL', 'SOME_API_KEY'):
            assert secret_key not in env, f"parent secret {secret_key} leaked into child env"
            assert sentinel_secret not in env.values(), 'sentinel secret value leaked into child env'

        # The 4 authoritative bridge vars are still present and correct.
        assert env['TG_BRIDGE_ENABLED'] == 'true'
        assert env['ALLOWED_USERS'] == '123'
        assert env['TG_TARGET_AGENT'] == 'Maine'
        # PATH is kept so the interpreter can launch (a legitimate essential).
        assert 'PATH' in env
    finally:
        for k in ('AWS_SECRET_ACCESS_KEY', 'DATABASE_URL', 'SOME_API_KEY'):
            os.environ.pop(k, None)


def test_healthy_run_resets_restart_debt(tmp_path):
    """Once the child has been alive >= healthy_window_sec, restart debt is cleared.

    Guards _reset_restart_debt_if_healthy(): a long-lived bridge must not keep
    accumulating consecutive-failure debt across its lifetime, otherwise it would
    eventually hit the cap and refuse to restart after a genuine transient blip.
    """
    sup = make_supervisor(tmp_path, healthy_window_sec=1.0)
    with sup._lock:
        # Simulate accumulated restart debt + a proc that started long enough ago.
        sup._restart_attempts = 3
        import time as _t
        sup._proc_started_at = _t.monotonic() - 5.0  # well past the 1s healthy window
        sup._reset_restart_debt_if_healthy()
        assert sup._restart_attempts == 0, 'healthy run must clear restart debt'

    with sup._lock:
        # But a freshly-started proc (not yet healthy) must NOT clear the debt.
        sup._restart_attempts = 2
        import time as _t
        sup._proc_started_at = _t.monotonic()  # just started
        sup._reset_restart_debt_if_healthy()
        assert sup._restart_attempts == 2, 'not-yet-healthy proc must keep restart debt'


def test_start_uses_actual_runtime_port_not_hardcoded(tmp_path, popen_patched):
    fake = FakeProcess()
    popen_patched['queue_fake'](fake)
    sup = make_supervisor(tmp_path)   # base_url http://127.0.0.1:8126
    try:
        sup.start()
    finally:
        sup.stop()
    assert popen_patched['popen'].call_args[1]['env']['AC_BASE_URL'] == 'http://127.0.0.1:8126'


# ---------------------------------------------------------------------------
# B. Idempotency (start/stop)
# ---------------------------------------------------------------------------

def test_start_idempotent_no_double_spawn(tmp_path, popen_patched):
    fake = FakeProcess()
    popen_patched['queue_fake'](fake)
    sup = make_supervisor(tmp_path)
    try:
        sup.start()
        sup.start()   # second call must be a no-op
        assert popen_patched['popen'].call_count == 1
        assert sup.is_running() is True
    finally:
        sup.stop()


def test_set_enabled_on_twice_spawns_once(tmp_path, popen_patched):
    fake = FakeProcess()
    popen_patched['queue_fake'](fake)
    sup = make_supervisor(tmp_path)
    try:
        sup.set_enabled(True)
        sup.set_enabled(True)   # UI re-sends full snapshot -> must not double-spawn
        assert popen_patched['popen'].call_count == 1
    finally:
        sup.stop()


def test_stop_when_not_running_is_safe_noop(tmp_path):
    sup = make_supervisor(tmp_path)
    sup.stop()   # nothing running — must not raise
    assert sup.is_running() is False
    assert sup.status()['enabled'] is False


def test_set_enabled_off_when_not_running_is_safe(tmp_path, popen_patched):
    sup = make_supervisor(tmp_path)
    sup.set_enabled(False)   # off when never started
    assert popen_patched['popen'].call_count == 0
    assert sup.is_running() is False


def test_set_enabled_off_stops_running_child(tmp_path, popen_patched):
    fake = FakeProcess()
    popen_patched['queue_fake'](fake)
    sup = make_supervisor(tmp_path)
    sup.set_enabled(True)
    assert popen_patched['popen'].call_count == 1
    sup.set_enabled(False)
    assert fake.terminated is True
    assert sup.is_running() is False


# ---------------------------------------------------------------------------
# C. Exit-code-aware restart policy
# ---------------------------------------------------------------------------

def test_exit_2_config_problem_no_restart(tmp_path, popen_patched):
    """Exit 2 = config problem -> PERMANENT, no restart, error surfaced."""
    fake = FakeProcess(exit_code=2)
    popen_patched['queue_fake'](fake)
    sup = make_supervisor(tmp_path)
    try:
        sup.set_enabled(True)
        # Watcher detects the exit-2 death and must NOT respawn.
        assert _wait_until(lambda: sup.is_running() is False), 'child should be dead'
        time.sleep(0.3)   # settle window to be sure no late restart happens
        assert popen_patched['popen'].call_count == 1, 'exit 2 must not trigger a restart'
        err = sup.status()['error']
        assert 'config problem' in err.lower() or 'exit 2' in err
    finally:
        sup.stop()


def test_exit_3_transient_restarts_with_cap(tmp_path, popen_patched):
    """Exit 3 = transient AC-open failure -> bounded restart, then give up at cap."""
    # max_restart_attempts=3 (make_supervisor default) -> total spawns = 1 + 3.
    for _ in range(5):
        popen_patched['queue_fake'](FakeProcess(exit_code=3))
    sup = make_supervisor(tmp_path)
    try:
        sup.set_enabled(True)
        # Wait until the cap is reached (4 spawns) or a generous timeout.
        assert _wait_until(lambda: popen_patched['popen'].call_count >= 4, timeout=10), \
            f"expected restarts up to cap, got {popen_patched['popen'].call_count}"
        time.sleep(0.3)   # let any (incorrect) further restarts surface
        assert popen_patched['popen'].call_count == 4, \
            f"expected exactly 1+cap spawns, got {popen_patched['popen'].call_count}"
        assert sup.is_running() is False
        err = sup.status()['error']
        assert 'giving up' in err.lower() or 'restart loop' in err
    finally:
        sup.stop()


def test_exit_0_clean_stop_no_restart(tmp_path, popen_patched):
    fake = FakeProcess(exit_code=0)
    popen_patched['queue_fake'](fake)
    sup = make_supervisor(tmp_path)
    try:
        sup.set_enabled(True)
        assert _wait_until(lambda: sup.is_running() is False), 'child should be dead'
        time.sleep(0.3)
        assert popen_patched['popen'].call_count == 1, 'clean exit 0 must not restart'
    finally:
        sup.stop()


def test_restart_respects_disable_during_backoff(tmp_path, popen_patched):
    """If the user disables while a transient failure is in flight, no restart happens."""
    fake = FakeProcess(exit_code=3)
    popen_patched['queue_fake'](fake)
    sup = make_supervisor(tmp_path)
    sup.set_enabled(True)
    # Immediately disable — watcher must not respawn.
    sup.set_enabled(False)
    time.sleep(0.3)
    assert popen_patched['popen'].call_count == 1


# ---------------------------------------------------------------------------
# D. stop() shutdown sequence (terminate -> wait -> kill)
# ---------------------------------------------------------------------------

def test_stop_terminates_and_waits(tmp_path, popen_patched):
    fake = FakeProcess()   # alive until terminated; terminate records the call
    popen_patched['queue_fake'](fake)
    sup = make_supervisor(tmp_path)
    sup.set_enabled(True)
    sup.stop()
    assert fake.terminated is True
    assert fake.killed is False       # process exited in time -> no kill needed
    assert any(t is not None for t in fake.wait_calls), 'stop() must wait after terminate'


def test_stop_escalates_to_kill_on_timeout(tmp_path, popen_patched):
    """Simulate a child that ignores terminate (still alive) -> stop() kills it."""
    import subprocess as _sp

    class StubbornProcess(FakeProcess):
        """A child that ignores terminate AND kill (stays alive forever)."""
        def terminate(self):
            self.terminated = True   # record the call, but do NOT reap (stay alive)
        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            # Always still alive -> raise TimeoutExpired to force the kill path.
            raise _sp.TimeoutExpired(cmd='bridge', timeout=timeout)

    stubborn = StubbornProcess()
    popen_patched['queue_fake'](stubborn)
    sup = make_supervisor(tmp_path)
    sup.set_enabled(True)
    sup.stop()
    assert stubborn.terminated is True
    assert stubborn.killed is True, 'stop() must kill() when terminate does not reap the child'


# ---------------------------------------------------------------------------
# E. Config handler
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
    # idempotent — verified separately in test_set_enabled_on_twice_spawns_once.
    assert sup.set_enabled.call_count == 2


def test_handler_noop_when_pool_none():
    _handle_telegram_bridge_enabled({'telegram_bridge_enabled': True}, None, [])   # must not raise


def test_handler_no_supervisor_attached_is_safe():
    pool = MagicMock(spec=[])   # no attributes at all -> hasattr checks fail safely
    pool.settings = PoolSettings()
    _handle_telegram_bridge_enabled({'telegram_bridge_enabled': True}, pool, [])   # must not raise
    assert pool.settings.telegram_bridge_enabled is True


# ---------------------------------------------------------------------------
# F. Persistence round-trip
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
# G. State serialization (both blocks must carry the field)
# ---------------------------------------------------------------------------

def test_state_builder_serializes_telegram_bridge_in_both_blocks():
    """Guard against the 'miss one of two blocks' gotcha: both dict literals in
    state_builder.py must expose telegram_bridge_enabled."""
    import inspect
    from agent_cascade.api_integration_pkg import state_builder as sb
    src = inspect.getsource(sb)
    count = src.count("'telegram_bridge_enabled'")
    assert count >= 2, f"expected telegram_bridge_enabled in both serialization blocks, found {count}"
