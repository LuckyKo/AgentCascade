"""Regression tests for BUG_0139 — git auto-gc "geometric repack" hang.

Background git auto-gc can outlive the primary command (commit/push/fetch) and
hold the shell's stdout/stderr pipes open until shell_cmd's timeout fires. The
fix has two parts:

1. ``build_shell_env()`` injects ``gc.auto=0`` through git's ``GIT_CONFIG_*``
   environment protocol for every shell the tool spawns, so auto-gc is not
   started by agent-driven commands.
2. When output still contains git's geometric-repack markers, timeout (sync and
   async) and non-zero sync completions append an actionable hint instead of a
   bare failure, because the primary git operation may already have succeeded.

These tests are fully controlled: no real git process is spawned.
"""

import os
import subprocess
from unittest.mock import MagicMock

import pytest

from agent_cascade.async_shell_pkg.task import AsyncShellTask
from agent_cascade.async_shell_pkg.tracker import AsyncShellTracker
from agent_cascade.operation_manager.shell import ShellMixin
from agent_cascade.shell_utils import (GIT_AUTOGC_HINT, build_shell_env, git_autogc_disabled_env,
                                       git_autogc_hint_for_output)


# ────────────────────────────────────────────────────────────────────────────────
# 1. Marker detection / hint text (pure)
# ────────────────────────────────────────────────────────────────────────────────
class TestGitAutogcHint:
    @pytest.mark.parametrize(
        'output',
        [
            'error: failed to perform geometric repack\nerror: task \'geometric-repack\' failed',
            'fatal: geometric-repack failed',
            'ERROR: GEOMETRIC REPACK did not finish',
        ],
    )
    def test_markers_return_hint(self, output):
        assert git_autogc_hint_for_output(output) == GIT_AUTOGC_HINT

    @pytest.mark.parametrize(
        'output',
        [
            '',
            None,
            'error: failed to push some refs',
            'geometric packing is fine',
        ],
    )
    def test_non_markers_return_empty(self, output):
        assert git_autogc_hint_for_output(output) == ''

    def test_hint_explains_may_have_succeeded_and_workaround(self):
        assert 'may already have succeeded' in GIT_AUTOGC_HINT
        assert 'git config gc.auto 0' in GIT_AUTOGC_HINT


# ────────────────────────────────────────────────────────────────────────────────
# 2. Environment injection (pure)
# ────────────────────────────────────────────────────────────────────────────────
class TestGitAutogcEnv:
    def test_adds_gc_auto_zero_entry(self):
        env = git_autogc_disabled_env({'PATH': '/bin'})
        assert env['PATH'] == '/bin'
        assert env['GIT_CONFIG_COUNT'] == '1'
        assert env['GIT_CONFIG_KEY_0'] == 'gc.auto'
        assert env['GIT_CONFIG_VALUE_0'] == '0'

    def test_appends_to_existing_config_entries(self):
        base = {
            'GIT_CONFIG_COUNT': '2',
            'GIT_CONFIG_KEY_0': 'user.name',
            'GIT_CONFIG_VALUE_0': 'agent',
            'GIT_CONFIG_KEY_1': 'core.autocrlf',
            'GIT_CONFIG_VALUE_1': 'false',
        }
        env = git_autogc_disabled_env(base)
        assert env['GIT_CONFIG_COUNT'] == '3'
        # Existing entries survive untouched.
        assert env['GIT_CONFIG_KEY_0'] == 'user.name'
        assert env['GIT_CONFIG_VALUE_1'] == 'false'
        # New entry is appended at the next slot.
        assert env['GIT_CONFIG_KEY_2'] == 'gc.auto'
        assert env['GIT_CONFIG_VALUE_2'] == '0'

    def test_malformed_existing_count_leaves_env_unchanged(self):
        env = git_autogc_disabled_env({'GIT_CONFIG_COUNT': 'not-a-number'})
        assert env == {'GIT_CONFIG_COUNT': 'not-a-number'}

    def test_build_shell_env_disables_autogc_by_default(self, monkeypatch):
        monkeypatch.delenv('AGENT_CASCADE_GIT_AUTO_GC', raising=False)
        monkeypatch.delenv('GIT_CONFIG_COUNT', raising=False)
        for key in list(os.environ):
            if key.startswith('GIT_CONFIG_KEY_') or key.startswith('GIT_CONFIG_VALUE_'):
                monkeypatch.delenv(key, raising=False)
        env = build_shell_env()
        assert env['GIT_CONFIG_COUNT'] == '1'
        assert env['GIT_CONFIG_KEY_0'] == 'gc.auto'
        assert env['GIT_CONFIG_VALUE_0'] == '0'

    def test_build_shell_env_kill_switch_preserves_autogc(self, monkeypatch):
        monkeypatch.setenv('AGENT_CASCADE_GIT_AUTO_GC', '1')
        monkeypatch.delenv('GIT_CONFIG_COUNT', raising=False)
        assert 'GIT_CONFIG_COUNT' not in build_shell_env()


# ────────────────────────────────────────────────────────────────────────────────
# 3. Sync wiring: fake process raising TimeoutExpired
# ────────────────────────────────────────────────────────────────────────────────
class _StaticPipe:
    def __init__(self, text=''):
        self._text = text

    def read(self, size=-1):
        text, self._text = self._text, ''
        return text


class _FakeProc:
    """Controllable Popen stand-in; never touches the host."""

    pid = 4242

    def __init__(self, *, stdout='', stderr='', returncode=0, timeout=False):
        self.stdout = _StaticPipe(stdout)
        self.stderr = _StaticPipe(stderr)
        self.returncode = returncode
        self._timeout = timeout

    def wait(self, timeout=None):
        if self._timeout:
            raise subprocess.TimeoutExpired(cmd='git', timeout=timeout)
        return self.returncode

    def kill(self):
        self.returncode = -1


def _make_om():
    from agent_cascade.operation_manager import OperationManager
    om = OperationManager.__new__(OperationManager)
    om.base_dir = '/tmp'
    om.agent_pool = MagicMock()
    return om


def _stub_sync_host(monkeypatch, proc):
    monkeypatch.setattr('agent_cascade.operation_manager.shell.ON_WINDOWS', False)
    monkeypatch.setattr(ShellMixin, 'evaluate_shell_command_safety', lambda self, *a, **k: (True, []))
    monkeypatch.setattr('agent_cascade.utils.tool_path_resolver.resolve_tool_path',
                        lambda *a, **k: '/tmp')
    # Never signal a real process group on POSIX hosts (the fake PID is not ours).
    monkeypatch.setattr(os, 'getpgid', lambda pid: pid, raising=False)
    monkeypatch.setattr(os, 'killpg', lambda pgid, sig: None, raising=False)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: proc)


GEOMETRIC_OUTPUT = "error: failed to perform geometric repack\nerror: task 'geometric-repack' failed\n"


class TestSyncWiring:
    def test_timeout_appends_hint(self, monkeypatch):
        proc = _FakeProc(stderr=GEOMETRIC_OUTPUT, returncode=-1, timeout=True)
        _stub_sync_host(monkeypatch, proc)
        result = _make_om().execute_shell_command(command='git fetch origin', justification='test',
                                                  agent_name='normal')
        assert 'Command timed out' in result
        assert GIT_AUTOGC_HINT in result

    def test_timeout_without_marker_has_no_hint(self, monkeypatch):
        proc = _FakeProc(stderr='still working\n', returncode=-1, timeout=True)
        _stub_sync_host(monkeypatch, proc)
        result = _make_om().execute_shell_command(command='git fetch origin', justification='test',
                                                  agent_name='normal')
        assert 'Command timed out' in result
        assert GIT_AUTOGC_HINT not in result

    def test_nonzero_completion_with_marker_appends_hint(self, monkeypatch):
        proc = _FakeProc(stderr=GEOMETRIC_OUTPUT, returncode=1)
        _stub_sync_host(monkeypatch, proc)
        result = _make_om().execute_shell_command(command='git commit -m x', justification='test',
                                                  agent_name='normal')
        assert 'return code 1' in result
        assert GIT_AUTOGC_HINT in result

    def test_success_has_no_hint_even_with_marker(self, monkeypatch):
        # rc=0 means nothing failed; no misleading hint.
        proc = _FakeProc(stderr=GEOMETRIC_OUTPUT, returncode=0)
        _stub_sync_host(monkeypatch, proc)
        result = _make_om().execute_shell_command(command='git status', justification='test',
                                                  agent_name='normal')
        assert GIT_AUTOGC_HINT not in result


# ────────────────────────────────────────────────────────────────────────────────
# 4. Async wiring: merged completion message includes the hint on timeout
# ────────────────────────────────────────────────────────────────────────────────
class TestAsyncWiring:
    def _make_tracker_with_task(self, lines):
        tracker = AsyncShellTracker(pool=None)
        task = AsyncShellTask(tool_id=1, agent_name='a', command='git fetch', timeout=30)
        task.stderr_lines.extend(lines)
        tracker._tasks.setdefault('a', {})[1] = task
        return tracker, task

    def test_timeout_completion_message_appends_hint(self):
        tracker, task = self._make_tracker_with_task(GEOMETRIC_OUTPUT.splitlines())
        msg = tracker._build_completion_message('a', 1, task, timed_out=True)
        assert 'Timed out after' in msg
        assert GIT_AUTOGC_HINT in msg

    def test_normal_completion_has_no_hint(self):
        tracker, task = self._make_tracker_with_task(GEOMETRIC_OUTPUT.splitlines())
        msg = tracker._build_completion_message('a', 1, task, timed_out=False)
        assert 'Completed in' in msg
        assert GIT_AUTOGC_HINT not in msg
