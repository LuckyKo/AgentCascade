"""BUG_0285: a sync shell_cmd must be cancellable while it blocks in proc.wait().

The incident: a 300 s sync pytest run kept running after the user pressed Stop,
then delivered a stale timeout result into the *next* turn, where it was
misattributed to the newer command.

Covered here:
  * user Stop (pool.stopped) kills the process tree mid-wait and returns a
    distinct "cancelled" message (NOT the timeout wording);
  * a newer run generation cancels too (Stop + new turn clears pool.stopped
    almost immediately, so the generation is the durable signal);
  * no Stop → normal completion path is unchanged;
  * SHELL_STOP_POLL_INTERVAL = 0 disables the watcher (documented off-state);
  * the Windows tree kill now uses taskkill /F /T on the first pass, while the
    WMIC leaf sweep stays plain /F.
"""

import os
import subprocess
import threading
import time

import pytest

from agent_cascade.operation_manager import shell as shell_mod
from agent_cascade.operation_manager.shell import ShellMixin


# ────────────────────────────────────────────────────────────────────────────────
# Test doubles (no real processes are spawned)
# ────────────────────────────────────────────────────────────────────────────────
class _StaticPipe:
    def __init__(self, text=''):
        self._text = text

    def read(self, size=-1):
        text, self._text = self._text, ''
        return text


class _BlockingProc:
    """Popen stand-in that only returns from wait() once killed."""

    def __init__(self, pid=6341, stdout='', stderr=''):
        self.pid = pid
        self.returncode = None
        self.stdout = _StaticPipe(stdout)
        self.stderr = _StaticPipe(stderr)
        self._killed = threading.Event()

    def wait(self, timeout=None):
        if self._killed.wait(timeout):
            return self.returncode
        raise subprocess.TimeoutExpired(cmd='blocking', timeout=timeout)

    def kill(self):
        self.returncode = -1
        self._killed.set()


class _FakePool:
    """Just the two stop signals _sync_shell_superseded reads."""

    def __init__(self, stopped=False, generation=7):
        self.stopped = stopped
        self._run_generation = generation


def _make_om(pool):
    from agent_cascade.operation_manager import OperationManager
    om = OperationManager.__new__(OperationManager)
    om.base_dir = '/tmp'
    om.agent_pool = pool
    return om


def _stub_sync_host(monkeypatch, proc, windows=False):
    """Wire execute_shell_command to the fake proc + fake platform."""
    monkeypatch.setattr(shell_mod, 'ON_WINDOWS', windows)
    monkeypatch.setattr(ShellMixin, 'evaluate_shell_command_safety', lambda self, *a, **k: (True, []))
    monkeypatch.setattr('agent_cascade.utils.tool_path_resolver.resolve_tool_path',
                        lambda *a, **k: '/tmp')
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: proc)


def _record_tree_kills(monkeypatch, proc):
    """Windows tree-kill stand-in: record the PID and actually stop the fake."""
    killed = []

    def _fake_tree_kill(self, pid, expect_root_exited=False):
        killed.append(pid)
        # Only a still-running fake needs unblocking (Stop/timeout path). On the
        # completion reap the proc already finished, so leave its returncode untouched —
        # exactly like the real dead-root kill, which cannot change an exited returncode.
        if proc.returncode is None:
            proc.kill()

    monkeypatch.setattr(ShellMixin, '_terminate_process_tree_windows', _fake_tree_kill)
    return killed


# ────────────────────────────────────────────────────────────────────────────────
# Cancellation behaviour
# ────────────────────────────────────────────────────────────────────────────────
class TestSyncShellStopCancellation:
    def test_user_stop_kills_tree_and_reports_cancellation(self, monkeypatch):
        proc = _BlockingProc(stdout='partial line\n')
        pool = _FakePool(stopped=False)
        _stub_sync_host(monkeypatch, proc, windows=True)
        killed = _record_tree_kills(monkeypatch, proc)

        def _press_stop():
            time.sleep(0.1)
            pool.stopped = True

        threading.Thread(target=_press_stop, daemon=True).start()

        started = time.time()
        result = _make_om(pool).execute_shell_command(
            command='python -m pytest tests/ -q', justification='test',
            agent_name='normal', timeout=300)
        elapsed = time.time() - started

        assert 'Command cancelled' in result
        assert 'user pressed Stop' in result
        assert 'timed out' not in result
        assert killed == [proc.pid]
        # Cancellation must land on the watcher cadence, not the 300 s timeout.
        assert elapsed < 5, f'cancel took {elapsed:.1f}s (timeout path?)'
        # Partial output captured before the kill is still surfaced.
        assert 'partial line' in result

    def test_newer_run_generation_cancels_even_when_stop_flag_cleared(self, monkeypatch):
        # A new turn starts right after Stop and clears pool.stopped; the
        # generation bump is what still marks this call as abandoned.
        proc = _BlockingProc()
        pool = _FakePool(stopped=False, generation=7)
        _stub_sync_host(monkeypatch, proc, windows=True)
        killed = _record_tree_kills(monkeypatch, proc)

        def _start_new_turn():
            time.sleep(0.1)
            pool._run_generation = 8
            pool.stopped = False  # _start_gen_thread clears it immediately

        threading.Thread(target=_start_new_turn, daemon=True).start()

        result = _make_om(pool).execute_shell_command(
            command='python -m pytest tests/ -q', justification='test',
            agent_name='normal', timeout=300)

        assert 'Command cancelled' in result
        assert killed == [proc.pid]

    def test_no_stop_completes_normally(self, monkeypatch):
        proc = _BlockingProc(stdout='done\n')
        proc.kill()  # process already finished
        proc.returncode = 0
        pool = _FakePool(stopped=False)
        _stub_sync_host(monkeypatch, proc, windows=True)
        killed = _record_tree_kills(monkeypatch, proc)

        result = _make_om(pool).execute_shell_command(
            command='git status', justification='test',
            agent_name='normal', timeout=300)

        assert 'Command completed successfully' in result
        assert 'cancelled' not in result
        # Reap-on-exit: even a command that finished cleanly sweeps the tree once
        # to collect any grandchild the wrapper detached. Exactly one dead-root pass.
        assert killed == [proc.pid]

    def test_poll_interval_zero_disables_watcher(self, monkeypatch):
        monkeypatch.setattr(shell_mod, 'SHELL_STOP_POLL_INTERVAL', 0)
        proc = _BlockingProc(stdout='done\n')
        proc.kill()
        proc.returncode = 0
        # Stop is already requested, but the watcher is off.
        pool = _FakePool(stopped=True)
        _stub_sync_host(monkeypatch, proc, windows=True)
        killed = _record_tree_kills(monkeypatch, proc)

        result = _make_om(pool).execute_shell_command(
            command='git status', justification='test',
            agent_name='normal', timeout=300)

        assert 'Command completed successfully' in result
        assert 'cancelled' not in result
        # Watcher disabled → no Stop path; normal completion still reaps once.
        assert killed == [proc.pid]


# ────────────────────────────────────────────────────────────────────────────────
# Windows tree-kill shape
# ────────────────────────────────────────────────────────────────────────────────
class TestTreeKillTaskkillFlags:
    def test_run_taskkill_tree_flag(self, monkeypatch):
        calls = []

        class _Completed:
            returncode = 0

        def _fake_run(args, **kwargs):
            calls.append(args)
            return _Completed()

        monkeypatch.setattr(shell_mod.subprocess, 'run', _fake_run)
        shell_mod._run_taskkill(123, tree=True)
        shell_mod._run_taskkill(456)

        assert calls[0] == ['taskkill', '/F', '/T', '/PID', '123']
        assert calls[1] == ['taskkill', '/F', '/PID', '456']

    def test_first_pass_uses_tree_and_leaf_sweep_does_not(self, monkeypatch):
        calls = []

        class _Completed:
            returncode = 0
            stdout = 'ProcessId\n987\n'

        def _fake_run(args, **kwargs):
            calls.append(args)
            return _Completed()

        monkeypatch.setattr(shell_mod.subprocess, 'run', _fake_run)
        monkeypatch.setattr(shell_mod.time, 'sleep', lambda s: None)

        om = _make_om(_FakePool())
        om._terminate_process_tree_windows(100)

        # Root kill carries /T so cmd.exe children die with it.
        assert calls[0] == ['taskkill', '/F', '/T', '/PID', '100']
        leaf_kills = [c for c in calls if '/PID' in c and c[-1] != '100']
        assert leaf_kills, 'expected WMIC-discovered leaf kills'
        # Leaf PIDs are killed WITHOUT /T (adopted children must not be swept).
        for leaf in leaf_kills:
            assert '/T' not in leaf


# ────────────────────────────────────────────────────────────────────────────────
# Reap-on-exit: dead-root-safe kill shape (expect_root_exited=True)
# ────────────────────────────────────────────────────────────────────────────────
class TestReapOnExitExpectRootExited:
    """The blocking completion path calls _terminate_shell_process(proc,
    expect_root_exited=True) because the shell wrapper has already exited. These
    mock-only tests pin the two dead-root behaviors that make that correct and
    quiet: Windows skips the dead-root taskkill passes but still sweeps descendants
    via WMIC; POSIX kills the group via the stored PID (getpgid would raise ESRCH
    once the group leader is reaped).
    """

    def test_windows_skips_root_taskkill_still_sweeps_wmic(self, monkeypatch):
        calls = []

        class _Completed:
            returncode = 0
            stdout = 'ProcessId\n987\n'

        def _fake_run(args, **kwargs):
            calls.append(list(args))
            return _Completed()

        monkeypatch.setattr(shell_mod.subprocess, 'run', _fake_run)
        monkeypatch.setattr(shell_mod.time, 'sleep', lambda s: None)

        _make_om(_FakePool())._terminate_process_tree_windows(100, expect_root_exited=True)

        # No dead-root taskkill on the wrapper PID (neither pass-1 /F /T nor pass-2 /F).
        root_kills = [c for c in calls if 'taskkill' in c and c[-1] == '100']
        assert root_kills == [], f"dead-root taskkill must be skipped, got: {root_kills}"
        # The WMIC descendant sweep still runs and the discovered leaf is killed.
        assert any('wmic' in c for c in calls), f"expected WMIC sweep, got: {calls}"
        assert ['taskkill', '/F', '/PID', '987'] in calls, \
            f"expected leaf kill for discovered descendant, got: {calls}"

    @pytest.mark.skipif(os.name != 'posix', reason='POSIX-only: os.killpg / signal.SIGKILL')
    def test_posix_kills_group_via_stored_pid_not_getpgid(self, monkeypatch):
        monkeypatch.setattr(shell_mod, 'ON_WINDOWS', False)
        killpg_calls = []
        monkeypatch.setattr(
            shell_mod.os, 'killpg', lambda pgid, sig: killpg_calls.append((pgid, sig)))

        def _getpgid_must_not_run(*a, **k):
            raise OSError('getpgid must not run on a reaped group leader')
        monkeypatch.setattr(shell_mod.os, 'getpgid', _getpgid_must_not_run)

        _make_om(_FakePool())._terminate_shell_process(
            _BlockingProc(pid=4242), expect_root_exited=True)

        assert killpg_calls == [(4242, shell_mod.signal.SIGKILL)], \
            f"expected killpg(stored_pid, SIGKILL), got: {killpg_calls}"

    @pytest.mark.skipif(os.name != 'posix', reason='POSIX-only: os.killpg / signal.SIGKILL')
    def test_posix_live_root_still_uses_getpgid(self, monkeypatch):
        # Regression guard: the default (live-root) POSIX path is unchanged.
        monkeypatch.setattr(shell_mod, 'ON_WINDOWS', False)
        killpg_calls = []
        monkeypatch.setattr(
            shell_mod.os, 'killpg', lambda pgid, sig: killpg_calls.append((pgid, sig)))
        monkeypatch.setattr(shell_mod.os, 'getpgid', lambda pid: pid + 1)

        _make_om(_FakePool())._terminate_shell_process(_BlockingProc(pid=100))

        assert killpg_calls == [(101, shell_mod.signal.SIGKILL)], \
            f"live-root path must use getpgid's result, got: {killpg_calls}"
