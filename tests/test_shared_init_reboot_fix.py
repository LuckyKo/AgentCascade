"""Regression test for the boot-time settings clobber in shared_init.py (the live entry path).

Root cause: ``shared_init.initialize_infrastructure`` read the 3 idle-timeout env vars with
HARDCODED defaults (900.0 / 900.0 / 60.0) and ``configure_and_start_pool`` applied all 3
unconditionally, then called ``_save_pool_settings()`` — so every boot via start_api_server.py
(→ start_multi_agent.py) stomped the user's persisted pool_settings.json values with the env/default.

Fix: ``_env_float`` returns None when the env var is ABSENT, and ``configure_and_start_pool``
only applies + persists the non-None keys. When nothing is overridden, the persisted values
survive boot (mirrors api_server.py main()'s guarded logic).

The existing tests/test_settings_reboot_fix.py drives the WRONG entry point (api_server.py
__main__ via runpy) and never touches configure_and_start_pool — that's why the suite stayed
green while the defect lived. These tests exercise the shared_init path directly.

Run:  pytest tests/test_shared_init_reboot_fix.py -v
"""

import inspect
from types import SimpleNamespace

from agent_cascade import shared_init
from agent_cascade.shared_init import _env_float, configure_and_start_pool

# The three env vars shared_init reads for the idle-timeout startup config.
IDLE_ENV_VARS = (
    'AGENT_CASCADE_IDLE_TIMEOUT',
    'AGENT_CASCADE_SYSTEM_AGENT_IDLE_TIMEOUT',
    'AGENT_CASCADE_IDLE_CHECK_INTERVAL',
)


class _FakeSettings:
    """Minimal stand-in for PoolSettings carrying the three startup-managed fields."""

    def __init__(self, **kw):
        self.idle_timeout_seconds = kw.get('idle_timeout_seconds', 1600.0)
        self.system_agent_idle_timeout_seconds = kw.get('system_agent_idle_timeout_seconds', 300.0)
        self.idle_check_interval = kw.get('idle_check_interval', 60.0)


class _FakePool:
    """Lightweight AgentPool stand-in tailored for configure_and_start_pool.

    ``settings`` carries the values that (in production) would have been loaded from
    pool_settings.json by AgentPool.__init__. ``operation_manager`` must be a real object
    because configure_and_start_pool sets ``agent_pool.operation_manager.agent_pool``.
    ``_save_pool_settings`` is a spy (``save_calls``) so we can assert the clobber save.
    """

    def __init__(self, **settings_kw):
        self.settings = _FakeSettings(**settings_kw)
        self.operation_manager = SimpleNamespace(agent_pool=None)
        self.save_calls = 0

    def _save_pool_settings(self):
        self.save_calls += 1

    def start(self):
        return None


def test_env_float_returns_value_when_set(monkeypatch):
    """_env_float yields the parsed float when the env var IS present."""
    monkeypatch.setenv('AGENT_CASCADE_SYSTEM_AGENT_IDLE_TIMEOUT', '600')
    assert _env_float('AGENT_CASCADE_SYSTEM_AGENT_IDLE_TIMEOUT') == 600.0


def test_env_float_returns_none_when_absent(monkeypatch):
    """_env_float yields None when the env var is ABSENT (no hardcoded default)."""
    for var in IDLE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    assert _env_float('AGENT_CASCADE_IDLE_TIMEOUT') is None
    assert _env_float('AGENT_CASCADE_SYSTEM_AGENT_IDLE_TIMEOUT') is None
    assert _env_float('AGENT_CASCADE_IDLE_CHECK_INTERVAL') is None


def test_no_env_leaves_persisted_values_untouched(monkeypatch):
    """(a) NO env var set → all-None startup config leaves persisted values UNCHANGED, no save."""
    for var in IDLE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    pool = _FakePool(
        idle_timeout_seconds=1000.0,
        system_agent_idle_timeout_seconds=300.0,
        idle_check_interval=45.0,
    )
    configure_and_start_pool(pool, None, None, None)

    # The user's persisted values must survive boot (not reset to any default).
    assert pool.settings.idle_timeout_seconds == 1000.0, \
        f"idle_timeout_seconds clobbered: {pool.settings.idle_timeout_seconds}"
    assert pool.settings.system_agent_idle_timeout_seconds == 300.0, \
        f"system idle clobbered: {pool.settings.system_agent_idle_timeout_seconds}"
    assert pool.settings.idle_check_interval == 45.0, \
        f"idle_check_interval clobbered: {pool.settings.idle_check_interval}"

    # No explicit override → no re-save → the file is NOT re-stamped.
    assert pool.save_calls == 0, \
        f"_save_pool_settings called {pool.save_calls}x with no override — file would be re-stamped"


def test_system_agent_env_override_applied_and_saved(monkeypatch):
    """(b) AGENT_CASCADE_SYSTEM_AGENT_IDLE_TIMEOUT=600 → _env_float yields 600.0 → applied + saved."""
    monkeypatch.setenv('AGENT_CASCADE_SYSTEM_AGENT_IDLE_TIMEOUT', '600')
    val = _env_float('AGENT_CASCADE_SYSTEM_AGENT_IDLE_TIMEOUT')
    assert val == 600.0

    pool = _FakePool(system_agent_idle_timeout_seconds=300.0)
    configure_and_start_pool(pool, None, val, None)

    # The env override wins and is applied to live settings...
    assert pool.settings.system_agent_idle_timeout_seconds == 600.0
    # ...and persisted so it survives future boots.
    assert pool.save_calls == 1, 'override was applied but not persisted'


def test_idle_timeout_env_override_applied_and_saved(monkeypatch):
    """(c) AGENT_CASCADE_IDLE_TIMEOUT=1200 → _env_float yields 1200.0 → applied + saved."""
    monkeypatch.setenv('AGENT_CASCADE_IDLE_TIMEOUT', '1200')
    val = _env_float('AGENT_CASCADE_IDLE_TIMEOUT')
    assert val == 1200.0

    pool = _FakePool(idle_timeout_seconds=1000.0)
    configure_and_start_pool(pool, val, None, None)

    assert pool.settings.idle_timeout_seconds == 1200.0
    assert pool.save_calls == 1


def test_idle_check_interval_env_override_applied_and_saved(monkeypatch):
    """(c) AGENT_CASCADE_IDLE_CHECK_INTERVAL=30 → _env_float yields 30.0 → applied + saved."""
    monkeypatch.setenv('AGENT_CASCADE_IDLE_CHECK_INTERVAL', '30')
    val = _env_float('AGENT_CASCADE_IDLE_CHECK_INTERVAL')
    assert val == 30.0

    pool = _FakePool(idle_check_interval=60.0)
    configure_and_start_pool(pool, None, None, val)

    assert pool.settings.idle_check_interval == 30.0
    assert pool.save_calls == 1


def test_partial_override_leaves_other_keys_untouched(monkeypatch):
    """Only the overridden key changes; the other two keep their persisted values, one save."""
    monkeypatch.setenv('AGENT_CASCADE_IDLE_TIMEOUT', '1200')
    pool = _FakePool(
        idle_timeout_seconds=1000.0,
        system_agent_idle_timeout_seconds=300.0,
        idle_check_interval=45.0,
    )
    configure_and_start_pool(pool, _env_float('AGENT_CASCADE_IDLE_TIMEOUT'), None, None)

    assert pool.settings.idle_timeout_seconds == 1200.0
    # Non-overridden keys are untouched.
    assert pool.settings.system_agent_idle_timeout_seconds == 300.0
    assert pool.settings.idle_check_interval == 45.0
    assert pool.save_calls == 1


def test_initialize_infrastructure_reads_env_without_hardcoded_defaults():
    """Producer guard: pin the ROOT CAUSE, not just the symptom.

    The clobber had two halves — initialize_infrastructure supplying hardcoded defaults AND
    configure_and_start_pool applying/saving them. This test asserts the producer reads the 3
    idle env vars via _env_float (None when absent) and NOT the old float(os.getenv(NAME,
    DEFAULT)) form, so a revert of the env reads cannot silently re-introduce the clobber while
    the suite stays green.
    """
    src = inspect.getsource(shared_init.initialize_infrastructure)
    for var in IDLE_ENV_VARS:
        assert f"_env_float('{var}')" in src, \
            f"initialize_infrastructure no longer reads {var} via _env_float"
        assert f"float(os.getenv('{var}'" not in src, \
            f"initialize_infrastructure reintroduced a hardcoded default for {var}"
