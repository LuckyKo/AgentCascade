"""Per-harness start spacing for ``call_cli_agent`` (provider anti-ban rate limit).

Some providers suspend an account that receives fast automated bursts, so one-shot run STARTS
are spaced by ``cli_agent_rate_limit_seconds`` (default 5 s, ``0`` = off). These tests cover:

  * the reservation math (a burst queues as t, t+interval, t+2*interval — never two in a window);
  * interval resolution (pool setting wins, non-numeric doubles fall back to the module default);
  * the two spawn sites (sync runner, async tracking thread) waiting BEFORE the container starts;
  * the status surface (``rate_limit_seconds`` / ``next_run_in_seconds``);
  * the config handler + registry entry, so the knob is persistable and settable.

No Docker, no real CLI, no production config is touched. Time is either injected (``now=`` /
``_sleep=``) or a tolerance-bounded real measurement.
"""
import io
import threading
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade import settings as app_settings
from agent_cascade.engine import cli_agent_runner as runner
from agent_cascade.settings_registry import spec


@pytest.fixture(autouse=True)
def _clean_clock(monkeypatch):
    """Per-test rate clock (module-global) + a deterministic module default for fallbacks."""
    monkeypatch.setattr(runner, '_LAST_START_AT', {})
    monkeypatch.setattr(app_settings, 'CLI_AGENT_RATE_LIMIT_SECONDS', 4.0)
    return runner._LAST_START_AT


# ──────────────────────────────────────────────────────────────────────────────
# 1) Reservation math
# ──────────────────────────────────────────────────────────────────────────────


class TestReserveStartSlot:
    def test_first_start_never_waits(self):
        assert runner._reserve_start_slot('h', 5, now=100.0) == 0.0

    def test_burst_is_spaced_from_reserved_instants(self):
        assert runner._reserve_start_slot('h', 5, now=100.0) == 0.0
        assert runner._reserve_start_slot('h', 5, now=100.0) == pytest.approx(5.0)
        assert runner._reserve_start_slot('h', 5, now=100.0) == pytest.approx(10.0)

    def test_late_caller_does_not_wait_but_pushes_the_next(self):
        runner._reserve_start_slot('h', 5, now=100.0)
        # 10 s later the window has long reopened: no wait, and the stamp moves to "now".
        assert runner._reserve_start_slot('h', 5, now=110.0) == 0.0
        assert runner._reserve_start_slot('h', 5, now=110.0) == pytest.approx(5.0)

    def test_clock_is_per_harness(self):
        runner._reserve_start_slot('a', 5, now=100.0)
        assert runner._reserve_start_slot('b', 5, now=100.0) == 0.0

    def test_zero_interval_disables_and_records_nothing(self):
        assert runner._reserve_start_slot('h', 0, now=100.0) == 0.0
        assert runner._LAST_START_AT == {}

    def test_negative_interval_disables(self):
        assert runner._reserve_start_slot('h', -1, now=100.0) == 0.0
        assert runner._LAST_START_AT == {}


class TestWaitForStartSlot:
    def test_waits_the_reserved_amount_via_injected_sleep(self):
        slept = []
        assert runner._wait_for_start_slot('h', 5, _sleep=slept.append) == 0.0
        assert slept == []  # first call starts immediately
        wait = runner._wait_for_start_slot('h', 5, _sleep=slept.append)
        assert wait == pytest.approx(5.0, abs=0.2)
        assert slept == [pytest.approx(5.0, abs=0.2)]

    def test_no_sleep_when_disabled(self):
        slept = []
        assert runner._wait_for_start_slot('h', 0, _sleep=slept.append) == 0.0
        assert slept == []


class TestNextStartIn:
    def test_reports_the_open_window_then_zero(self):
        runner._reserve_start_slot('h', 5)  # real clock
        assert 0.0 < runner._next_start_in('h', 5) <= 5.0
        assert runner._next_start_in('h', 0) == 0.0
        assert runner._next_start_in('other', 5) == 0.0


# ──────────────────────────────────────────────────────────────────────────────
# 2) Interval resolution
# ──────────────────────────────────────────────────────────────────────────────


class TestIntervalResolution:
    def test_pool_setting_wins(self):
        assert runner._rate_limit_interval(NS(cli_agent_rate_limit_seconds=2.5)) == 2.5
        assert runner._rate_limit_interval(NS(cli_agent_rate_limit_seconds=0)) == 0.0

    def test_missing_or_non_numeric_falls_back_to_module_default(self):
        assert runner._rate_limit_interval(NS()) == app_settings.CLI_AGENT_RATE_LIMIT_SECONDS
        assert runner._rate_limit_interval(MagicMock()) == app_settings.CLI_AGENT_RATE_LIMIT_SECONDS
        assert runner._rate_limit_interval(None) == app_settings.CLI_AGENT_RATE_LIMIT_SECONDS

    def test_bool_is_not_accepted_as_a_number(self):
        # True/False would otherwise coerce to 1/0 and silently change the spacing.
        assert runner._rate_limit_interval(
            NS(cli_agent_rate_limit_seconds=True)) == app_settings.CLI_AGENT_RATE_LIMIT_SECONDS


# ──────────────────────────────────────────────────────────────────────────────
# 3) Spawn sites: the wait must happen BEFORE the container starts
# ──────────────────────────────────────────────────────────────────────────────


def _prepared(harness='claude_code'):
    return runner.PreparedCliAgentRun(
        early_result=None, docker_cmd=['docker', 'run', '--rm', 'img'], container_name='c',
        clean_env={}, cap=2, harness=harness, caller_name='caller',
        requested_posture='acceptEdits')


def test_sync_runner_waits_before_spawn(monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(runner, '_LOGS_DIR', tmp_path)
    monkeypatch.setattr(runner, '_persist_log', lambda **kw: 'log://x')
    monkeypatch.setattr(runner, 'prepare_cli_agent_run', lambda **kw: _prepared())
    monkeypatch.setattr(runner, '_wait_for_start_slot',
                        lambda harness, interval: order.append(('wait', harness, interval)) or 0.0)

    def fake_run(cmd, **kw):
        order.append(('spawn',))
        return MagicMock(stdout='ok', stderr='', returncode=0)

    monkeypatch.setattr(runner.subprocess, 'run', fake_run)
    pool = NS(settings=NS(cli_agent_rate_limit_seconds=7.0))

    res = runner.run_cli_agent(harness='claude_code', task='t', workspace=str(tmp_path),
                               permission_posture='acceptEdits', timeout=10, pool=pool,
                               caller_name='caller')
    assert res.exit_code == 0
    assert order == [('wait', 'claude_code', 7.0), ('spawn',)]


class _FakeProc:
    """Minimal Popen stand-in with iterable pipes (drain threads must terminate)."""

    def __init__(self, pid=4321):
        self.pid = pid
        self.returncode = 0
        self.stdout = io.StringIO('')
        self.stderr = io.StringIO('')

    def poll(self):
        return 0

    def kill(self):
        pass


def test_async_tracker_waits_before_spawn(monkeypatch):
    from agent_cascade.async_shell_pkg.cli_agent_tracker import CliAgentTracker
    from agent_cascade.async_shell_pkg.task import AsyncShellTask

    pool = NS(settings=NS(cli_agent_rate_limit_seconds=7.0))
    tracker = CliAgentTracker(pool=pool)
    task = AsyncShellTask(tool_id=1, agent_name='test_agent', command='run-1')
    ctx = {'docker_cmd': ['docker', 'run', 'img'], 'container_name': 'c', 'clean_env': {},
           'timeout': 60, 'caller_name': 'caller', 'harness': 'antigravity',
           'requested_posture': 'acceptEdits', 'cap': 2}
    with tracker._lock:
        tracker._launch_ctx['run-1'] = ctx
        tracker._tasks.setdefault('test_agent', {})[1] = task

    order = []
    monkeypatch.setattr(runner, '_wait_for_start_slot',
                        lambda harness, interval: order.append(('wait', harness, interval)) or 0.0)
    sem = threading.Semaphore(2)
    with patch('agent_cascade.async_shell_pkg.cli_agent_tracker.subprocess.Popen',
               return_value=_FakeProc()) as popen, \
         patch('agent_cascade.engine.cli_agent_runner._get_semaphore', return_value=sem):
        tracker._spawn_process('test_agent', 1, 'run-1', None)

    # Same harness+interval the sync path uses, and it runs before the container is launched.
    assert order == [('wait', 'antigravity', 7.0)]
    popen.assert_called_once()
    assert sem._value == 1  # still held while the run is in flight


def test_killed_while_queued_does_not_spawn(monkeypatch):
    """A kill during the rate-limit wait must not start a doomed container (that is still a
    provider request). The slot is released and the placeholder stays for kill handling."""
    from agent_cascade.async_shell_pkg.cli_agent_tracker import CliAgentTracker
    from agent_cascade.async_shell_pkg.task import AsyncShellTask

    tracker = CliAgentTracker(pool=NS(settings=NS(cli_agent_rate_limit_seconds=7.0)))
    task = AsyncShellTask(tool_id=1, agent_name='test_agent', command='run-k')
    task.killed = True
    ctx = {'docker_cmd': ['docker', 'run', 'img'], 'container_name': 'c', 'clean_env': {},
           'timeout': 60, 'caller_name': 'caller', 'harness': 'antigravity',
           'requested_posture': 'acceptEdits', 'cap': 2}
    with tracker._lock:
        tracker._launch_ctx['run-k'] = ctx
        tracker._tasks.setdefault('test_agent', {})[1] = task

    monkeypatch.setattr(runner, '_wait_for_start_slot', lambda harness, interval: 0.0)
    sem = threading.Semaphore(2)
    with patch('agent_cascade.async_shell_pkg.cli_agent_tracker.subprocess.Popen') as popen, \
         patch('agent_cascade.engine.cli_agent_runner._get_semaphore', return_value=sem):
        assert tracker._spawn_process('test_agent', 1, 'run-k', None) is task

    popen.assert_not_called()
    assert sem._value == 2  # slot released, never handed to the task
    assert getattr(task, '_cli_sem', None) is None


# ──────────────────────────────────────────────────────────────────────────────
# 4) Status surface
# ──────────────────────────────────────────────────────────────────────────────


def test_status_reports_spacing_and_window(monkeypatch):
    import config.secrets_loader as sl
    monkeypatch.setattr(sl, 'get_secret', lambda name: None)
    monkeypatch.setattr(runner, '_image_present_cached', lambda tag: True)
    monkeypatch.setattr(runner, '_agy_auth_volume_populated', lambda vol, image: True)
    pool = NS(settings=NS(cli_agent_enabled={}, cli_agent_images={}, cli_agent_models={},
                          cli_agent_paths_ro=False, cli_agent_agy_auth_volume='vol-1',
                          cli_agent_rate_limit_seconds=6.0))

    for entry in runner.cli_agent_status(pool).values():
        assert entry['rate_limit_seconds'] == 6.0
        assert entry['next_run_in_seconds'] == 0.0

    runner._reserve_start_slot('antigravity', 6.0)
    again = runner.cli_agent_status(pool)
    assert 0.0 < again['antigravity']['next_run_in_seconds'] <= 6.0
    # Other harnesses are unaffected (per-harness clock).
    assert again['claude_code']['next_run_in_seconds'] == 0.0


# ──────────────────────────────────────────────────────────────────────────────
# 5) Config handler + registry
# ──────────────────────────────────────────────────────────────────────────────


class TestConfigHandler:
    def _pool(self):
        return NS(settings=NS(cli_agent_rate_limit_seconds=5.0))

    def test_sets_value(self):
        from agent_cascade.config_handlers import _handle_cli_agent_rate_limit_seconds
        pool = self._pool()
        _handle_cli_agent_rate_limit_seconds({'cli_agent_rate_limit_seconds': 12.5}, pool, [])
        assert pool.settings.cli_agent_rate_limit_seconds == 12.5

    def test_zero_is_off(self):
        from agent_cascade.config_handlers import _handle_cli_agent_rate_limit_seconds
        pool = self._pool()
        _handle_cli_agent_rate_limit_seconds({'cli_agent_rate_limit_seconds': 0}, pool, [])
        assert pool.settings.cli_agent_rate_limit_seconds == 0.0

    def test_negative_clamps_to_zero(self):
        from agent_cascade.config_handlers import _handle_cli_agent_rate_limit_seconds
        pool = self._pool()
        _handle_cli_agent_rate_limit_seconds({'cli_agent_rate_limit_seconds': -3}, pool, [])
        assert pool.settings.cli_agent_rate_limit_seconds == 0.0

    def test_unparsable_keeps_current_value(self):
        from agent_cascade.config_handlers import _handle_cli_agent_rate_limit_seconds
        pool = self._pool()
        _handle_cli_agent_rate_limit_seconds({'cli_agent_rate_limit_seconds': 'fast'}, pool, [])
        assert pool.settings.cli_agent_rate_limit_seconds == 5.0


def test_registry_entry_is_numeric_and_persisted():
    from agent_cascade.agent_instance import PoolSettings
    s = spec('cli_agent_rate_limit_seconds')
    # Registry default is derived from the PoolSettings field (single source of truth).
    assert s.default == PoolSettings.cli_agent_rate_limit_seconds
    assert isinstance(s.default, float) and s.default > 0
    assert s.py_type == (int, float)
    assert s.persist is True and s.save_trigger is True
