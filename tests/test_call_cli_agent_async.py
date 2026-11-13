"""Tests for async/background execution of ``call_cli_agent`` (CliAgentTracker).

Mirrors the mock-Popen pattern of tests/test_async_shell_cmd.py, specialised for the
one-shot ``docker run`` container lifecycle. Covers the two load-bearing risks:

  * TRUST-WRAP: async completion is enqueued as a raw USER-role message, so the tracker
    must apply ``_wrap_untrusted('call_cli_agent', body)`` itself (and no-op when the flag
    is off).
  * SEMAPHORE LIFETIME: the container-concurrency semaphore must be acquired in the spawn
    path and held until the container exits (released exactly once in ``_record_completed_run``).

No Docker, no real CLI, no LLM.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.async_shell_pkg import CliAgentTracker
from agent_cascade.async_shell_pkg.task import AsyncShellTask
from agent_cascade.tool_dispatcher import ToolDispatcher
from agent_cascade.engine import tool_execution


# ──────────────────────────────────────────────────────────────────────────────
# Shared fakes
# ──────────────────────────────────────────────────────────────────────────────


class _FakeProc:
    """Stand-in for subprocess.Popen of a one-shot docker container.

    ``finished=False`` keeps ``poll()`` returning None (running); ``kill()``/``set_finished()``
    flip it so kill teardown and completion paths settle promptly. stdout/stderr are StringIO so
    the real drain threads populate the task buffers.
    """

    def __init__(self, rc=0, stdout='', stderr='', finished=False, pid=9999):
        import io
        self.pid = pid
        self._rc = rc
        self._finished = finished
        self.returncode = rc if finished else None
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)

    def poll(self):
        return self._rc if self._finished else None

    def kill(self):
        self._finished = True
        self.returncode = self._rc

    def set_finished(self):
        self.kill()

    def wait(self, timeout=None):
        return self._rc


class _RecordingPool:
    """Minimal pool: captures enqueued completion messages (the USER-role delivery route)."""

    def __init__(self):
        self.enqueued = []

    def enqueue_message(self, agent_name, text):
        self.enqueued.append((agent_name, text))


def _ctx(container_name='container_x', cap=2):
    return {
        'docker_cmd': ['docker', 'run', '--rm', container_name], 'container_name': container_name,
        'clean_env': {}, 'timeout': 60, 'caller_name': 'test_agent', 'harness': 'claude_code',
        'requested_posture': 'acceptEdits', 'cap': cap,
    }


def _add_task(tracker, task, ctx=None):
    """Register a task (and its launch-context) on the tracker for direct-method tests."""
    with tracker._lock:
        if ctx is not None:
            tracker._launch_ctx[task.command] = ctx
        tracker._tasks.setdefault(task.agent_name, {})[task.tool_id] = task


def _running_task(tool_id=1, **kw):
    return AsyncShellTask(tool_id=tool_id, agent_name='test_agent',
                          command=kw.pop('command', 'run-id'), **kw)


@pytest.fixture(autouse=True)
def _fast_intervals(monkeypatch):
    """Shrink the poll/early-check intervals and neutralise side effects for determinism."""
    monkeypatch.setattr('agent_cascade.async_shell_pkg.tracker.HEARTBEAT_CHECK_INTERVAL', 0.01)
    monkeypatch.setattr('agent_cascade.async_shell_pkg.tracker.EARLY_OUTPUT_CHECK_TIMEOUT', 0.01)
    monkeypatch.setattr('agent_cascade.utils.wakeup_helpers.relaunch_idle_agent', lambda *a, **k: None)
    # Persist is a file-write concern; stub it to a sentinel so completion messages carry it
    # without touching the filesystem.
    monkeypatch.setattr('agent_cascade.engine.cli_agent_runner._persist_log',
                        lambda **kw: 'log://test')


# ──────────────────────────────────────────────────────────────────────────────
# 1) launch returns immediately with zero heartbeats
# ──────────────────────────────────────────────────────────────────────────────


def test_launch_returns_id_immediately_no_heartbeats():
    pool = _RecordingPool()
    tracker = CliAgentTracker(pool=pool)
    proc = _FakeProc(finished=False)  # stays running
    with patch('agent_cascade.async_shell_pkg.cli_agent_tracker.subprocess.Popen', return_value=proc), \
         patch('agent_cascade.async_shell_pkg.cli_agent_tracker.AsyncShellTracker._send_heartbeat') as hb:
        tool_id, pid, completed_early, rc, log_path, early_out = tracker.launch(
            agent_name='test_agent', docker_cmd=['docker', 'run'], container_name='c', clean_env={},
            timeout=60, caller_name='test_agent', harness='claude_code', requested_posture='acceptEdits', cap=2)
        assert tool_id == 1
        assert completed_early is False
        assert log_path == ''
        # Completion-only invariant: heartbeat interval is forced to -1.
        task = tracker._get_task('test_agent', tool_id)
        assert task.heartbeat_interval == -1.0
        assert hb.call_count == 0  # never emits a heartbeat
        tracker.kill_all('test_agent')  # settle the tracking thread
    import time
    for _ in range(100):  # killed path delivers completion then reaps; bounded wait
        if not tracker.has_active_tasks('test_agent'):
            break
        time.sleep(0.02)
    assert tracker.has_active_tasks('test_agent') is False


# ──────────────────────────────────────────────────────────────────────────────
# 2) completion → exactly one trust-wrapped ⟨call_cli_agent completed⟩ message
# ──────────────────────────────────────────────────────────────────────────────


def _drive_completion(tracker, monkeypatch):
    """Run a fast-completing container through _track_task and return enqueued messages."""
    proc = _FakeProc(rc=0, stdout='hi there', stderr='', finished=True)
    task = _running_task(command='run-c')
    task.launch_check_done.set()  # direct _track_task call: no launch() to set it
    ctx = _ctx()
    _add_task(tracker, task, ctx)
    with patch('agent_cascade.async_shell_pkg.cli_agent_tracker.subprocess.Popen', return_value=proc):
        tracker._track_task('test_agent', 1, 'run-c', None)
    return tracker


def test_completion_message_trust_wrapped_on(monkeypatch):
    monkeypatch.setattr(tool_execution, 'UNTRUSTED_TOOL_OUTPUT_WRAPPER', True)
    from agent_cascade.engine.tool_execution import UNTRUSTED_TOOL_OUTPUT_TAG as TAG
    pool = _RecordingPool()
    tracker = CliAgentTracker(pool=pool)
    _drive_completion(tracker, monkeypatch)
    msgs = [m for (a, m) in pool.enqueued if a == 'test_agent']
    assert len(msgs) == 1, f"expected exactly one completion message, got {len(msgs)}"
    body = msgs[0]
    assert body.startswith(f'<{TAG}>') and body.endswith(f'</{TAG}>'), 'trust delimiters missing'
    assert '⟨call_cli_agent completed⟩' in body
    assert 'full_output=log://test' in body


def test_completion_message_unwrapped_when_flag_off(monkeypatch):
    monkeypatch.setattr(tool_execution, 'UNTRUSTED_TOOL_OUTPUT_WRAPPER', False)
    from agent_cascade.engine.tool_execution import UNTRUSTED_TOOL_OUTPUT_TAG as TAG
    pool = _RecordingPool()
    tracker = CliAgentTracker(pool=pool)
    _drive_completion(tracker, monkeypatch)
    body = pool.enqueued[0][1]
    assert '⟨call_cli_agent completed⟩' in body
    assert TAG not in body, 'must not wrap when the flag is off'


# ──────────────────────────────────────────────────────────────────────────────
# 3) __kill → docker kill <name>
# ──────────────────────────────────────────────────────────────────────────────


def test_kill_leads_with_docker_kill():
    tracker = CliAgentTracker(pool=None)
    proc = _FakeProc(finished=False)
    task = _running_task(pid=9999)
    task.process = proc
    _add_task(tracker, task, _ctx(container_name='ctr42'))
    with patch('agent_cascade.engine.cli_agent_runner._kill_container') as kc:
        msg = tracker.kill_task('test_agent', 1)
    kc.assert_called_once_with('ctr42')
    assert proc.poll() is not None  # client backstop flipped it dead
    assert 'killed' in msg.lower()


# ──────────────────────────────────────────────────────────────────────────────
# 4) hard timeout → timed_out, rc==124, docker kill invoked
# ──────────────────────────────────────────────────────────────────────────────


def test_timeout_normalizes_rc_to_124_and_docker_kills():
    pool = _RecordingPool()
    tracker = CliAgentTracker(pool=pool)
    proc = _FakeProc(finished=False)
    task = _running_task(command='run-t')
    task.timeout = 0.02
    task.launch_check_done.set()
    _add_task(tracker, task, _ctx(container_name='ctr-timeout'))
    with patch('agent_cascade.async_shell_pkg.cli_agent_tracker.subprocess.Popen', return_value=proc), \
         patch('agent_cascade.engine.cli_agent_runner._kill_container') as kc:
        tracker._track_task('test_agent', 1, 'run-t', None)
    kc.assert_called()  # timeout teardown leads with docker kill
    body = pool.enqueued[0][1]
    assert 'exit=124' in body, f"timeout header should report exit=124: {body!r}"
    run = tracker._get_completed_run('test_agent', 1)
    assert run is not None and run['return_code'] == 124


# ──────────────────────────────────────────────────────────────────────────────
# 5) __status on running + reaped task
# ──────────────────────────────────────────────────────────────────────────────


def test_status_running_and_reaped_fallback():
    tracker = CliAgentTracker(pool=None)
    task = _running_task(command='run-s')
    task.process = _FakeProc(finished=False, pid=9999)
    task.stdout_lines = ['partial output']
    _add_task(tracker, task, _ctx())

    running = tracker.get_status('test_agent', 1)
    assert running.startswith('⟨call_cli_agent status⟩')
    assert 'running' in running
    assert 'partial output' in running

    # Reap: record the completed run then remove the live task.
    task.completed = True
    task.return_code = 0
    tracker._record_completed_run('test_agent', 1, task)
    with tracker._lock:
        tracker._tasks['test_agent'].pop(1, None)
    reaped = tracker.get_status('test_agent', 1)
    assert reaped.startswith('⟨call_cli_agent status⟩')
    assert 'Process already completed' in reaped


# ──────────────────────────────────────────────────────────────────────────────
# 6) control routing hits the cli tracker, not the shell tracker (isolation)
# ──────────────────────────────────────────────────────────────────────────────


def _dispatcher_with_trackers(cli_tracker, shell_tracker):
    pool = MagicMock()
    pool._cli_agent_tracker = cli_tracker
    pool._async_shell_tracker = shell_tracker
    d = ToolDispatcher(pool)
    d.set_engine(MagicMock())
    inst = MagicMock()
    inst.instance_name = 'Maine'
    return d, inst


def test_control_routing_isolated_and_guidance():
    cli = MagicMock()
    cli.get_status.return_value = 'CLI-STATUS'
    cli.kill_task.return_value = 'CLI-KILLED'
    shell = MagicMock()
    d, inst = _dispatcher_with_trackers(cli, shell)

    assert d.handle_call_cli_agent({'tool_id': 5, 'task': '__status', 'execution_mode': 'async'}, inst) == 'CLI-STATUS'
    cli.get_status.assert_called_once_with('Maine', 5)
    assert d.handle_call_cli_agent({'tool_id': 5, 'task': '__kill', 'execution_mode': 'async'}, inst) == 'CLI-KILLED'
    cli.kill_task.assert_called_once_with('Maine', 5)
    # Isolation: the shell tracker must never be consulted for cli control commands.
    shell.get_status.assert_not_called()
    shell.kill_task.assert_not_called()

    # Unsupported verb -> guidance, no tracker action beyond the above.
    guidance = d.handle_call_cli_agent({'tool_id': 5, 'task': '__restart'}, inst)
    assert '__status' in guidance and '__kill' in guidance


# ──────────────────────────────────────────────────────────────────────────────
# 7) has_active_tasks stays active until completion_delivered (BUG_0156)
# ──────────────────────────────────────────────────────────────────────────────


def test_has_active_tasks_until_delivered():
    tracker = CliAgentTracker(pool=None)
    task = _running_task()
    _add_task(tracker, task, _ctx())
    assert tracker.has_active_tasks('test_agent') is True  # running
    with task._lock:
        task.completed = True  # process finished, result still in flight
    assert tracker.has_active_tasks('test_agent') is True  # undelivered -> still active
    CliAgentTracker._mark_completion_delivered(task)
    assert tracker.has_active_tasks('test_agent') is False  # delivered -> not active


# ──────────────────────────────────────────────────────────────────────────────
# 8) semaphore held across the background run (oversubscription regression guard)
# ──────────────────────────────────────────────────────────────────────────────


def test_semaphore_held_across_run_and_released_once():
    tracker = CliAgentTracker(pool=None)
    sem = threading.Semaphore(2)
    task = _running_task(command='run-sem')
    _add_task(tracker, task, _ctx(cap=2))
    proc = _FakeProc(finished=True)
    with patch('agent_cascade.async_shell_pkg.cli_agent_tracker.subprocess.Popen', return_value=proc), \
         patch('agent_cascade.engine.cli_agent_runner._get_semaphore', return_value=sem):
        tracker._spawn_process('test_agent', 1, 'run-sem', None)
        # Acquired in the spawn path, NOT released at launch() return.
        assert sem._value == 1, 'semaphore must be held while the container runs'
        assert task._cli_sem is sem
        # Released exactly once when the run is recorded (post-exit).
        tracker._record_completed_run('test_agent', 1, task)
        assert sem._value == 2, 'semaphore must be released after the container exits'
        assert task._cli_sem is None  # prevents double-release
        # Reentrant release must not over-release.
        tracker._record_completed_run('test_agent', 1, task)
        assert sem._value == 2


def test_semaphore_released_on_spawn_error():
    tracker = CliAgentTracker(pool=None)
    sem = threading.Semaphore(2)
    task = _running_task(command='run-err')
    _add_task(tracker, task, _ctx(cap=2))
    with patch('agent_cascade.async_shell_pkg.cli_agent_tracker.subprocess.Popen',
               side_effect=OSError('docker not found')), \
         patch('agent_cascade.engine.cli_agent_runner._get_semaphore', return_value=sem):
        with pytest.raises(OSError):
            tracker._spawn_process('test_agent', 1, 'run-err', None)
    assert sem._value == 2, 'spawn failure must release the semaphore it acquired'
    # No _cli_sem handed to the task -> _record_completed_run won't double-release.
    assert getattr(task, '_cli_sem', None) is None
    tracker._record_completed_run('test_agent', 1, task)
    assert sem._value == 2


# ──────────────────────────────────────────────────────────────────────────────
# 9) Popen raises → error-route completion is trust-wrapped + cli label
# ──────────────────────────────────────────────────────────────────────────────


def test_spawn_error_route_wrapped(monkeypatch):
    monkeypatch.setattr(tool_execution, 'UNTRUSTED_TOOL_OUTPUT_WRAPPER', True)
    from agent_cascade.engine.tool_execution import UNTRUSTED_TOOL_OUTPUT_TAG as TAG
    pool = _RecordingPool()
    tracker = CliAgentTracker(pool=pool)
    task = _running_task(command='run-boom')
    task.launch_check_done.set()
    _add_task(tracker, task, _ctx())
    sem = threading.Semaphore(2)
    with patch('agent_cascade.async_shell_pkg.cli_agent_tracker.subprocess.Popen',
               side_effect=OSError('no docker')), \
         patch('agent_cascade.engine.cli_agent_runner._get_semaphore', return_value=sem):
        tracker._track_task('test_agent', 1, 'run-boom', None)
    msgs = [m for (a, m) in pool.enqueued if a == 'test_agent']
    assert len(msgs) == 1
    body = msgs[0]
    assert body.startswith(f'<{TAG}>'), 'error route must be trust-wrapped too'
    assert '⟨call_cli_agent completed⟩' in body
    assert sem._value == 2  # spawn-error release path keeps the slot free
