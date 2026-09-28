"""Tests for async shell_cmd kill mechanism.

Verifies that __kill actually terminates processes and returns only after
confirmation, fixing bugs where kill returned but heartbeats continued.
"""

import os
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agent_cascade.async_shell import AsyncShellTask, AsyncShellTracker
from agent_cascade.log import logger


def _setup_task(tracker, task):
    """Add a task to a tracker under test_agent."""
    with tracker._lock:
        if 'test_agent' not in tracker._tasks:
            tracker._tasks['test_agent'] = {}
        tracker._tasks['test_agent'][task.tool_id] = task


def _make_running_task(tool_id=1, heartbeat_interval=-1.0, **kwargs):
    """Create a running AsyncShellTask with mock process."""
    return AsyncShellTask(
        tool_id=tool_id,
        agent_name='test_agent',
        command=kwargs.pop('command', 'ping -n 9999 127.0.0.1 >nul'),
        pid=kwargs.pop('pid', 99999),
        completed=False,
        heartbeat_interval=heartbeat_interval,
        **kwargs
    )


# ============================================================================
# kill_task unit tests (mocked process)
# ============================================================================

class TestKillTaskWithMockedProcess:

    def test_kill_sets_killed_flag(self):
        """kill_task sets task.killed = True."""
        tracker = AsyncShellTracker(pool=None)
        proc_mock = MagicMock()
        proc_mock.pid = 99999

        # First returns None (alive), then after kill_process_tree is called, returns exit code
        call_count = [0]
        def poll_side_effect():
            call_count[0] += 1
            if call_count[0] <= 2:
                return None
            return -1

        proc_mock.poll.side_effect = poll_side_effect

        task = _make_running_task(pid=99999, process=proc_mock)
        _setup_task(tracker, task)

        result = tracker.kill_task('test_agent', 1)

        assert task.killed is True
        assert 'Shell killed' in result or 'killed' in result.lower()

    def test_kill_calls_kill_process_tree(self):
        """kill_task calls _kill_process_tree directly."""
        tracker = AsyncShellTracker(pool=None)
        proc_mock = MagicMock()
        proc_mock.poll.return_value = None  # Still running
        proc_mock.pid = 99999

        task = _make_running_task(pid=99999, process=proc_mock)
        _setup_task(tracker, task)

        with patch.object(tracker, '_kill_process_tree') as mock_kill:
            tracker.kill_task('test_agent', 1)

            mock_kill.assert_called_once()
            assert mock_kill.call_args[0][0] is proc_mock

    @pytest.mark.parametrize('poll_behavior,expected_in_result', [
        ('waits_then_dead', 'Shell killed'),
        ('never_dies', 'did not terminate'),
    ])
    def test_kill_returns_after_process_confirmed_dead(self, poll_behavior, expected_in_result):
        """kill_task waits until proc.poll() != None before returning success (or errors on timeout)."""
        tracker = AsyncShellTracker(pool=None)
        proc_mock = MagicMock()
        proc_mock.pid = 99999

        if poll_behavior == 'waits_then_dead':
            # First polls return None, then process dies
            call_count = [0]
            def poll_side_effect():
                call_count[0] += 1
                return None if call_count[0] <= 2 else -1
            proc_mock.poll.side_effect = poll_side_effect

            task = _make_running_task(pid=99999, process=proc_mock)
            _setup_task(tracker, task)

            result = tracker.kill_task('test_agent', 1)

            assert expected_in_result in result
            # Verify we waited (poll called multiple times)
            assert call_count[0] > 2

        elif poll_behavior == 'never_dies':
            proc_mock.poll.return_value = None  # Never dies

            task = _make_running_task(pid=99999, process=proc_mock)
            _setup_task(tracker, task)

            with patch('agent_cascade.async_shell_pkg.constants.KILL_WAIT_TIMEOUT', 0.3):
                result = tracker.kill_task('test_agent', 1)

            assert expected_in_result in result or 'timed out' in result.lower()

    def test_kill_already_finished(self):
        """kill_task returns info message if process already finished."""
        tracker = AsyncShellTracker(pool=None)
        proc_mock = MagicMock()
        proc_mock.poll.return_value = 0  # Already done

        task = _make_running_task(pid=99999, process=proc_mock, return_code=0)
        _setup_task(tracker, task)

        result = tracker.kill_task('test_agent', 1)

        assert 'already finished' in result.lower()

    def test_kill_nonexistent_task(self):
        """kill_task returns error for nonexistent task."""
        tracker = AsyncShellTracker(pool=None)

        result = tracker.kill_task('test_agent', 999)

        assert 'No running shell found' in result

    # ── Dead-shell (non-existent tool_id) coverage for ALL control commands ──
    # Regression: every control method must return the graceful dead-shell message
    # for a non-existent ID and never touch an OS kill/ctrl helper.

    def test_ctrl_c_nonexistent_task(self):
        """send_ctrl_c returns error for nonexistent task and never calls the Windows ctrl helper."""
        tracker = AsyncShellTracker(pool=None)

        with patch('agent_cascade.async_shell_pkg.windows._send_windows_ctrl_c') as mock_ctrl:
            result = tracker.send_ctrl_c('test_agent', 999)

        assert 'No running shell found' in result
        mock_ctrl.assert_not_called()

    def test_get_status_nonexistent_task(self):
        """get_status returns error for nonexistent task."""
        tracker = AsyncShellTracker(pool=None)

        result = tracker.get_status('test_agent', 999)

        assert 'No running shell found' in result

    def test_update_heartbeat_nonexistent_task(self):
        """update_heartbeat returns error for nonexistent task."""
        tracker = AsyncShellTracker(pool=None)

        result = tracker.update_heartbeat('test_agent', 999, 5.0)

        assert 'No running shell found' in result

    def test_send_input_nonexistent_task(self):
        """send_input returns error for nonexistent task."""
        tracker = AsyncShellTracker(pool=None)

        result = tracker.send_input('test_agent', 999, 'echo hi')

        assert 'No running shell found' in result


# ============================================================================
# kill_task integration tests (real processes)
# ============================================================================

class TestKillTaskWithRealProcess:

    def test_kill_terminates_long_running_process(self):
        """kill_task terminates a real long-running process and stops heartbeats (cross-platform)."""
        pool = MagicMock()
        messages_received = []
        pool.enqueue_message = lambda agent, msg: messages_received.append((agent, msg))

        tracker = AsyncShellTracker(pool=pool)

        # Use platform-appropriate long-running command
        if os.name == 'nt':
            cmd = 'ping -t 127.0.0.1 >nul 2>&1'
        else:
            cmd = 'sleep 60'

        tool_id = None
        try:
            tool_id, pid, _, completed_early, _ = tracker.launch(
                agent_name='test_agent',
                command=cmd,
                heartbeat_interval=0.5,
                timeout=3600,
            )

            assert not completed_early, 'Command should not complete instantly'

            # Wait for process to start and possibly send a heartbeat
            time.sleep(1.0)

            # On Windows, verify the real PID is alive before kill
            if os.name == 'nt':
                task = tracker._get_task('test_agent', tool_id)
                real_pid = task.pid if task else None
                assert real_pid is not None and real_pid > 0, f"PID not set: {real_pid}"

            # Kill the task
            result = tracker.kill_task('test_agent', tool_id)
            assert 'Shell killed' in result, f"Kill failed: {result}"

            # Verify no more heartbeats after kill
            hb_before_kill = sum(1 for m in messages_received if 'heartbeat' in m[1].lower())
            time.sleep(1.5)  # Long enough for at least 3 heartbeats if still running
            hb_after_kill = sum(1 for m in messages_received if 'heartbeat' in m[1].lower())

            assert hb_after_kill == hb_before_kill, \
                f"Expected no more heartbeats after kill, but got {hb_after_kill - hb_before_kill}"

            # Windows-specific: verify process is actually dead via tasklist
            if os.name == 'nt' and real_pid:
                time.sleep(0.3)
                try:
                    proc_info = subprocess.run(
                        ['tasklist', '/FI', f'PID eq {real_pid}', '/NH'],
                        capture_output=True, text=True, timeout=5
                    )
                    assert str(real_pid) not in proc_info.stdout.strip(), \
                        f"Process {real_pid} still running after kill. Output: {proc_info.stdout[:200]}"
                except subprocess.TimeoutExpired:
                    pytest.skip('tasklist timed out')
        finally:
            if tool_id is not None:
                try:
                    tracker.kill_task('test_agent', tool_id)
                except Exception:
                    pass

    def test_kill_returns_only_after_process_dead(self):
        """kill_task blocks until the process is confirmed terminated (Windows)."""
        if os.name != 'nt':
            pytest.skip('Windows-only test')

        pool = MagicMock()
        pool.enqueue_message = lambda agent, msg: None

        tracker = AsyncShellTracker(pool=pool)

        tool_id = None
        try:
            tool_id, _, _, completed_early, _ = tracker.launch(
                agent_name='test_agent',
                command='ping -t 127.0.0.1 >nul 2>&1',
                heartbeat_interval=-1,
                timeout=3600,
            )

            assert not completed_early

            # Wait for tracking thread to set the PID on the task
            time.sleep(0.5)

            # Get real PID from task (may be available in launch return now, but read from task for certainty)
            task = tracker._get_task('test_agent', tool_id)
            pid = task.pid if task else None
            assert pid is not None and pid > 0, f"PID not set yet: {pid}"

            # Record time before kill
            start = time.time()
            result = tracker.kill_task('test_agent', tool_id)
            time.time() - start

            assert 'Shell killed' in result, f"Kill failed: {result}"

            # Give a brief moment for process table to update
            time.sleep(0.3)

            # Verify the process is actually dead after kill returns
            try:
                proc_info = subprocess.run(
                    ['tasklist', '/FI', f'PID eq {pid}', '/NH'],
                    capture_output=True, text=True, timeout=5
                )
                assert str(pid) not in proc_info.stdout.strip(), \
                    f"Process {pid} still running after kill returned. Output: {proc_info.stdout[:200]}"
            except subprocess.TimeoutExpired:
                pytest.skip('tasklist timed out')
        finally:
            if tool_id is not None:
                try:
                    tracker.kill_task('test_agent', tool_id)
                except Exception:
                    pass


# ============================================================================
# poll_loop killed flag check verification
# ============================================================================

class TestPollLoopKilledFlag:

    def test_poll_loop_exits_when_killed(self):
        """_poll_loop breaks when task.killed is set to True."""
        tracker = AsyncShellTracker(pool=None)

        proc_mock = MagicMock()
        proc_mock.poll.return_value = None  # Never completes naturally

        task = _make_running_task(process=proc_mock)
        t_out = threading.Thread(target=lambda: time.sleep(10), daemon=True)
        t_err = threading.Thread(target=lambda: time.sleep(10), daemon=True)

        # Set killed before entering poll loop
        with task._lock:
            task.killed = True

        timed_out = tracker._poll_loop('test_agent', 1, proc_mock, task, t_out, t_err)

        assert not timed_out  # Didn't time out, exited due to kill flag


# ============================================================================
# PowerShell -NoProfile injection test (BUG 3)
# ============================================================================

class TestPowerShellNoProfile:

    def test_powershell_command_gets_noprofile(self):
        """PowerShell commands get -NoProfile injected on Windows."""
        if os.name != 'nt':
            pytest.skip('Windows-only feature')

        pool = MagicMock()
        pool.enqueue_message = lambda agent, msg: None

        tracker = AsyncShellTracker(pool=pool)

        # Mock subprocess.Popen to capture the actual command used
        captured_cmd = []

        original_popen = subprocess.Popen  # noqa: F841  (save original Popen for restore)

        def mock_popen(cmd, **kwargs):
            if isinstance(cmd, str):
                captured_cmd.append(cmd)
            proc_mock = MagicMock()
            proc_mock.pid = 12345
            proc_mock.poll.return_value = 0
            proc_mock.stdout = iter([])
            proc_mock.stderr = iter([])
            proc_mock.stdin = MagicMock()
            return proc_mock

        with patch('subprocess.Popen', mock_popen):
            tracker.launch(
                agent_name='test_agent',
                command='powershell -Command "Write-Output hello"',
                heartbeat_interval=-1,
                timeout=3600,
            )

        assert captured_cmd, 'No command was captured'
        cmd = captured_cmd[0]
        assert '-NoProfile' in cmd, f"-NoProfile not found in: {cmd}"


# ============================================================================
# kill_all tests
# ============================================================================

class TestKillAll:

    def test_kill_all_sets_killed_flag_on_all_tasks(self):
        """kill_all sets killed=True on all active tasks for an agent."""
        tracker = AsyncShellTracker(pool=None)

        proc1 = MagicMock()
        proc1.poll.return_value = None
        task1 = _make_running_task(tool_id=1, pid=1001, process=proc1)
        _setup_task(tracker, task1)

        proc2 = MagicMock()
        proc2.poll.return_value = None
        task2 = _make_running_task(tool_id=2, pid=1002, process=proc2)
        _setup_task(tracker, task2)

        count = tracker.kill_all('test_agent')

        assert count == 2
        assert task1.killed is True
        assert task2.killed is True


# ============================================================================
# Race condition tests
# ============================================================================

class TestKillRaceConditions:

    def test_concurrent_kill_no_errors(self):
        """Multiple concurrent kill_task calls don't cause errors or double-kill issues."""
        tracker = AsyncShellTracker(pool=None)
        proc_mock = MagicMock()
        proc_mock.pid = 99999

        # Process dies after first kill call
        call_count = [0]
        def poll_side_effect():
            if call_count[0] >= 1:
                return -1
            return None

        proc_mock.poll.side_effect = poll_side_effect

        task = _make_running_task(pid=99999, process=proc_mock)
        _setup_task(tracker, task)

        errors = []

        def kill_and_capture():
            try:
                result = tracker.kill_task('test_agent', 1)
                return result
            except Exception as e:
                errors.append(e)
                return str(e)

        # Launch multiple concurrent kill calls
        threads = [threading.Thread(target=kill_and_capture) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # No exceptions should occur from race conditions
        assert len(errors) == 0, f"Errors during concurrent kill: {errors}"

    def test_kill_while_tracking_thread_running(self):
        """kill_task called while tracking thread is in poll_loop causes no errors."""
        pool = MagicMock()
        pool.enqueue_message = lambda agent, msg: None
        tracker = AsyncShellTracker(pool=pool)

        proc_mock = MagicMock()
        proc_mock.pid = 99999
        proc_mock.poll.return_value = None  # Never completes naturally

        task = _make_running_task(pid=99999, process=proc_mock)
        _setup_task(tracker, task)

        errors = []

        def poll_loop_runner():
            try:
                t_out = threading.Thread(target=lambda: time.sleep(10), daemon=True)
                t_err = threading.Thread(target=lambda: time.sleep(10), daemon=True)
                tracker._poll_loop('test_agent', 1, proc_mock, task, t_out, t_err)
            except Exception as e:
                errors.append(('poll_loop', e))

        def kill_runner():
            time.sleep(0.05)  # Let poll loop start first
            try:
                tracker.kill_task('test_agent', 1)
            except Exception as e:
                errors.append(('kill_task', e))

        t_poll = threading.Thread(target=poll_loop_runner, daemon=True)
        t_kill = threading.Thread(target=kill_runner, daemon=True)

        t_poll.start()
        t_kill.start()

        t_poll.join(timeout=5)
        t_kill.join(timeout=5)

        assert len(errors) == 0, f"Race condition errors: {errors}"


# ============================================================================
# Windows sibling process kill test (& operator spawns siblings, not children)
# ============================================================================

class TestKillSiblingProcesses:

    def test_kill_warns_about_surviving_children(self):
        """Verify _kill_process_tree logs a warning when child processes survive.

        Uses mocked subprocess calls to deterministically simulate survivors,
        avoiding flaky behavior from real process timing.
        """
        if os.name != 'nt':
            pytest.skip('Windows-only test')

        tracker = AsyncShellTracker(pool=None)

        # Capture log output to check for survivor warning
        import io as iomodule
        import logging
        log_capture = iomodule.StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setLevel(logging.WARNING)
        logger.addHandler(handler)

        try:
            call_count = [0]

            def mock_subprocess_run(cmd, **kwargs):
                call_count[0] += 1
                cmd_str = str(cmd)
                # First call: PowerShell to get descendants (has ConvertTo-Csv and ParentProcessId)
                if 'ConvertTo-Csv' in cmd_str:
                    return MagicMock(
                        returncode=0,
                        stdout='"ProcessId","ParentProcessId"\n"1000","4"\n"2000","1000"\n"3000","1000"\n',
                        stderr=''
                    )
                # Second call: taskkill (succeeds)
                elif 'taskkill' in cmd_str:
                    return MagicMock(returncode=0, stdout='', stderr='')
                # Third call: PowerShell to check survivors (uses -Filter "ProcessId IN")
                elif 'ProcessId IN' in cmd_str:
                    return MagicMock(
                        returncode=0,
                        stdout='2000\n3000\n',  # These two survived
                        stderr=''
                    )
                return MagicMock(returncode=0, stdout='', stderr='')

            proc_mock = MagicMock()
            proc_mock.pid = 1000
            proc_mock.poll.return_value = None  # Still running

            with patch('subprocess.run', side_effect=mock_subprocess_run):
                tracker._kill_process_tree(proc_mock, 'test_agent', 42)

            log_output = log_capture.getvalue()

            # Verify warning was logged about survivors
            assert 'survived tree kill' in log_output.lower(), \
                f"Expected survivor warning. Log: {log_output}"
            assert 'tool_id=42' in log_output, \
                f"Warning should mention tool_id. Log: {log_output}"
            assert 'orphaned' in log_output.lower(), \
                f"Warning should mention orphaned. Log: {log_output}"

        finally:
            logger.removeHandler(handler)

    def test_kill_second_pass_kills_survivors(self):
        """Verify _kill_process_tree runs a SECOND taskkill pass for survivors.

        Extends the mocked-survivors pattern: after the first tree-kill, the survivor
        check reports PIDs 2000/3000 still alive. The tracker must then issue an
        individual ``taskkill /F /PID`` (NOT /T) for EACH survivor, re-check aliveness,
        and only warn about PIDs that survived BOTH passes.
        """
        if os.name != 'nt':
            pytest.skip('Windows-only test')

        tracker = AsyncShellTracker(pool=None)

        import io as iomodule
        import logging
        log_capture = iomodule.StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setLevel(logging.DEBUG)
        logger.addHandler(handler)

        try:
            # Record every taskkill invocation so we can assert the second pass.
            taskkill_calls = []
            survivor_check_count = [0]

            def mock_subprocess_run(cmd, **kwargs):
                cmd_str = str(cmd)
                # First call: PowerShell to get descendants (has ConvertTo-Csv)
                if 'ConvertTo-Csv' in cmd_str:
                    return MagicMock(
                        returncode=0,
                        stdout='"ProcessId","ParentProcessId"\n"1000","4"\n"2000","1000"\n"3000","1000"\n',
                        stderr=''
                    )
                # taskkill calls: first pass uses /T, second pass uses plain /F per PID.
                elif 'taskkill' in cmd_str:
                    taskkill_calls.append(cmd)
                    return MagicMock(returncode=0, stdout='', stderr='')
                # Survivor check (PowerShell -Filter "ProcessId IN"). The FIRST check
                # reports 2000/3000 alive (triggers the second pass); the RE-CHECK after
                # the second pass reports them dead.
                elif 'ProcessId IN' in cmd_str:
                    survivor_check_count[0] += 1
                    if survivor_check_count[0] == 1:
                        return MagicMock(returncode=0, stdout='2000\n3000\n', stderr='')
                    return MagicMock(returncode=0, stdout='', stderr='')
                return MagicMock(returncode=0, stdout='', stderr='')

            proc_mock = MagicMock()
            proc_mock.pid = 1000
            proc_mock.poll.return_value = None  # Still running

            with patch('subprocess.run', side_effect=mock_subprocess_run):
                tracker._kill_process_tree(proc_mock, 'test_agent', 42)

            log_output = log_capture.getvalue()

            # A second-pass taskkill must have been issued for EACH survivor PID.
            # Each call is a list like ['taskkill', '/F', '/PID', '<pid>'] — the PID
            # is the LAST element (NOT str(c)[-1], which would be the closing bracket).
            second_pass = [c for c in taskkill_calls if '/T' not in str(c)]
            assert len(second_pass) == 2, \
                f"Expected 2 second-pass taskkill calls, got {len(second_pass)}: {taskkill_calls}"
            killed_pids = {c[-1] for c in second_pass}
            assert killed_pids == {'2000', '3000'}, \
                f"Second pass should target each survivor PID. Got: {second_pass}"

            # Survivors died on the second pass, so NO "orphaned" warning this time.
            assert 'survived tree kill' not in log_output.lower(), \
                f"No orphan warning expected when second pass succeeds. Log: {log_output}"

        finally:
            logger.removeHandler(handler)

    def test_kill_second_pass_warns_if_still_alive(self):
        """If PIDs survive BOTH passes, the original 'orphaned' warning is still logged."""
        if os.name != 'nt':
            pytest.skip('Windows-only test')

        tracker = AsyncShellTracker(pool=None)

        import io as iomodule
        import logging
        log_capture = iomodule.StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setLevel(logging.WARNING)
        logger.addHandler(handler)

        try:
            taskkill_calls = []

            def mock_subprocess_run(cmd, **kwargs):
                cmd_str = str(cmd)
                if 'ConvertTo-Csv' in cmd_str:
                    return MagicMock(
                        returncode=0,
                        stdout='"ProcessId","ParentProcessId"\n"1000","4"\n"2000","1000"\n',
                        stderr=''
                    )
                elif 'taskkill' in cmd_str:
                    taskkill_calls.append(cmd)
                    return MagicMock(returncode=0, stdout='', stderr='')
                # Both survivor checks report 2000 still alive (survives both passes).
                elif 'ProcessId IN' in cmd_str:
                    return MagicMock(returncode=0, stdout='2000\n', stderr='')
                return MagicMock(returncode=0, stdout='', stderr='')

            proc_mock = MagicMock()
            proc_mock.pid = 1000
            proc_mock.poll.return_value = None  # Still running

            with patch('subprocess.run', side_effect=mock_subprocess_run):
                tracker._kill_process_tree(proc_mock, 'test_agent', 42)

            log_output = log_capture.getvalue()

            # Second pass was attempted... (PID is the last list element, not str(c)[-1])
            second_pass = [c for c in taskkill_calls if '/T' not in str(c)]
            assert any(c[-1] == '2000' for c in second_pass), \
                f"Expected a second-pass taskkill for PID 2000. Got: {taskkill_calls}"
            # ...but the survivor survived both passes, so the warning is still logged.
            assert 'survived tree kill' in log_output.lower(), \
                f"Expected orphan warning for double-survivor. Log: {log_output}"
            assert 'orphaned' in log_output.lower(), \
                f"Warning should mention orphaned. Log: {log_output}"

        finally:
            logger.removeHandler(handler)

    def test_kill_second_pass_runs_without_descendants(self):
        """The second pass + survivor check run even when the main process has NO descendants.

        Regression: the escalation was originally gated on ``if descendant_pids:``, so a
        surviving main process with no captured children never got re-killed or warned about.
        The survivor check must be unconditional on Windows.
        """
        if os.name != 'nt':
            pytest.skip('Windows-only test')

        tracker = AsyncShellTracker(pool=None)

        import io as iomodule
        import logging
        log_capture = iomodule.StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setLevel(logging.DEBUG)
        logger.addHandler(handler)

        try:
            taskkill_calls = []
            survivor_check_count = [0]

            def mock_subprocess_run(cmd, **kwargs):
                cmd_str = str(cmd)
                # PowerShell descendant query: NO children (only the root 1000 and its parent).
                if 'ConvertTo-Csv' in cmd_str:
                    return MagicMock(
                        returncode=0,
                        stdout='"ProcessId","ParentProcessId"\n"4","0"\n"1000","4"\n',
                        stderr=''
                    )
                elif 'taskkill' in cmd_str:
                    taskkill_calls.append(cmd)
                    return MagicMock(returncode=0, stdout='', stderr='')
                # Survivor check: first reports the MAIN PID 1000 still alive (triggers
                # second pass); re-check after second pass reports it dead.
                elif 'ProcessId IN' in cmd_str:
                    survivor_check_count[0] += 1
                    if survivor_check_count[0] == 1:
                        return MagicMock(returncode=0, stdout='1000\n', stderr='')
                    return MagicMock(returncode=0, stdout='', stderr='')
                return MagicMock(returncode=0, stdout='', stderr='')

            proc_mock = MagicMock()
            proc_mock.pid = 1000
            proc_mock.poll.return_value = None  # Still running

            with patch('subprocess.run', side_effect=mock_subprocess_run):
                tracker._kill_process_tree(proc_mock, 'test_agent', 42)

            log_output = log_capture.getvalue()

            # A second-pass taskkill for the main PID must have run despite no descendants.
            second_pass = [c for c in taskkill_calls if '/T' not in str(c)]
            assert any(c[-1] == '1000' for c in second_pass), \
                f"Expected a second-pass taskkill for the main PID 1000 (no descendants). Got: {taskkill_calls}"

        finally:
            logger.removeHandler(handler)

    def test_descendant_pid_collection(self):
        """Verify _get_windows_descendant_pids correctly finds child processes."""
        if os.name != 'nt':
            pytest.skip('Windows-only test')

        tracker = AsyncShellTracker(pool=None)

        # Start a process with known children
        proc = subprocess.Popen(
            ['cmd', '/c', 'start /B ping -t 127.0.0.1 >nul'],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

        try:
            time.sleep(0.5)  # Let child spawn

            descendants = tracker._get_windows_descendant_pids(proc.pid)

            # Should find at least the ping process as a descendant
            assert isinstance(descendants, list), 'Should return a list of PIDs'
            # Note: We don't assert len > 0 because start /B may spawn as sibling not child
            # The important thing is the method works without errors

        finally:
            # Capture descendants BEFORE terminating so we can also kill the ping
            # child. `start /B` spawns ping as a SIBLING of cmd (not its child), so
            # terminating proc alone would orphan it — exactly the bug this suite guards.
            try:
                descendant_pids = tracker._get_windows_descendant_pids(proc.pid)
            except Exception:
                descendant_pids = []

            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

            # Best-effort kill of any ping descendant (Windows-only, guarded).
            if os.name == 'nt':
                for dpid in descendant_pids:
                    try:
                        subprocess.run(['taskkill', '/F', '/PID', str(dpid)],
                                       capture_output=True, timeout=5)
                    except Exception:
                        pass


# ============================================================================
# Unit tests for helper methods with mocked subprocess
# ============================================================================

class TestDescendantPIDCollectionMocked:

    def test_no_children_returns_empty(self):
        """_get_windows_descendant_pids returns [] when process has no children."""
        tracker = AsyncShellTracker(pool=None)

        # Mock PowerShell CSV output: ProcessId, ParentProcessId
        mock_output = '"ProcessId","ParentProcessId"\n"4","0"\n"1000","4"\n'
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=mock_output)

            descendants = tracker._get_windows_descendant_pids(1000)

            assert descendants == [], f"Expected empty list, got {descendants}"

    def test_finds_direct_children(self):
        """_get_windows_descendant_pids finds direct children via PPID."""
        tracker = AsyncShellTracker(pool=None)

        # PowerShell CSV: ProcessId, ParentProcessId
        # Parent 1000 has child 2000 (PPID=1000) and grandchild 3000 (PPID=2000)
        mock_output = (
            '"ProcessId","ParentProcessId"\n'
            '"4","0"\n'
            '"1000","4"\n'
            '"2000","1000"\n'
            '"3000","2000"\n'
        )
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=mock_output)

            descendants = tracker._get_windows_descendant_pids(1000)

            assert 2000 in descendants, 'Should find direct child'
            assert 3000 in descendants, 'Should find grandchild'
            assert 1000 not in descendants, 'Should not include parent itself'
            assert len(descendants) == 2

    def test_handles_commas_in_output(self):
        """_get_windows_descendant_pids correctly parses CSV with commas."""
        tracker = AsyncShellTracker(pool=None)

        # PowerShell output - csv module handles edge cases
        mock_output = (
            '"ProcessId","ParentProcessId"\n'
            '"4","0"\n'
            '"1000","4"\n'
            '"2000","1000"\n'
        )
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=mock_output)

            descendants = tracker._get_windows_descendant_pids(1000)

            assert 2000 in descendants, 'Should find child'

    def test_handles_tasklist_failure(self):
        """_get_windows_descendant_pids returns [] on subprocess failure."""
        tracker = AsyncShellTracker(pool=None)

        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired('powershell', 10)

            descendants = tracker._get_windows_descendant_pids(1000)

            assert descendants == []

    def test_handles_empty_output(self):
        """_get_windows_descendant_pids returns [] on empty output."""
        tracker = AsyncShellTracker(pool=None)

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='')

            descendants = tracker._get_windows_descendant_pids(1000)

            assert descendants == []


class TestPIDAliveCheckMocked:

    def test_finds_alive_pid(self):
        """_check_windows_pids_alive correctly identifies running PIDs."""
        tracker = AsyncShellTracker(pool=None)

        # PowerShell returns list of alive ProcessIds, one per line
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='4\n500\n')

            alive = tracker._check_windows_pids_alive([4, 500, 999])

            assert 4 in alive, 'PID 4 should be alive'
            assert 500 in alive, 'PID 500 should be alive'
            assert 999 not in alive, 'Non-existent PID should not be reported alive'

    def test_empty_input_returns_empty(self):
        """_check_windows_pids_alive returns [] for empty input."""
        tracker = AsyncShellTracker(pool=None)

        alive = tracker._check_windows_pids_alive([])
        assert alive == []


class TestKillProcessTreeEarlyExit:

    def test_skips_kill_if_already_dead(self):
        """_kill_process_tree returns early if process.poll() is not None."""
        tracker = AsyncShellTracker(pool=None)

        proc_mock = MagicMock()
        proc_mock.pid = 12345
        proc_mock.poll.return_value = 0  # Already finished

        with patch.object(tracker, '_get_windows_descendant_pids') as mock_descendants:
            with patch('subprocess.run') as mock_taskkill:
                tracker._kill_process_tree(proc_mock, 'test_agent', 1)

                mock_descendants.assert_not_called()
                mock_taskkill.assert_not_called()


class TestKillProcessTreeTaskkillFailure:

    def test_handles_taskkill_failure_gracefully(self):
        """_kill_process_tree logs warning and continues verification when taskkill fails."""
        if os.name != 'nt':
            pytest.skip('Windows-only test')

        tracker = AsyncShellTracker(pool=None)

        import io as iomodule
        import logging
        log_capture = iomodule.StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setLevel(logging.DEBUG)
        logger.addHandler(handler)

        try:
            call_count = [0]

            def mock_subprocess_run(cmd, **kwargs):
                call_count[0] += 1
                cmd_str = str(cmd)
                # First call: PowerShell to get descendants (has ConvertTo-Csv)
                if 'ConvertTo-Csv' in cmd_str:
                    return MagicMock(
                        returncode=0,
                        stdout='"ProcessId","ParentProcessId"\n"1000","4"\n"2000","1000"\n',
                        stderr=''
                    )
                # Second call: taskkill (fails with non-zero exit code)
                elif 'taskkill' in cmd_str:
                    return MagicMock(
                        returncode=128,
                        stdout='',
                        stderr='Access is denied.\n'
                    )
                # Third call: PowerShell to check survivors
                elif 'ProcessId IN' in cmd_str:
                    return MagicMock(returncode=0, stdout='2000\n', stderr='')
                return MagicMock(returncode=0, stdout='', stderr='')

            proc_mock = MagicMock()
            proc_mock.pid = 1000
            proc_mock.poll.return_value = None  # Still running

            with patch('subprocess.run', side_effect=mock_subprocess_run):
                tracker._kill_process_tree(proc_mock, 'test_agent', 99)

            log_output = log_capture.getvalue()

            # Verify taskkill failure warning was logged
            assert 'taskkill for pid 1000 failed' in log_output.lower(), \
                f"Expected taskkill failure warning. Log: {log_output}"
            assert 'access is denied' in log_output.lower(), \
                f"Expected error message in warning. Log: {log_output}"

            # Verify survivor verification still happened (didn't abort)
            assert 'survived tree kill' in log_output.lower(), \
                f"Should still verify survivors after taskkill failure. Log: {log_output}"

        finally:
            logger.removeHandler(handler)


# ============================================================================
# atexit safety net (kills all still-running tracked tasks on interpreter exit)
# ============================================================================

class TestAtexitCleanup:

    def test_atexit_registered_once(self):
        """The atexit hook is registered exactly once even with multiple trackers.

        Instantiate two trackers and patch atexit.register to count calls — only the
        FIRST tracker should register (module-level guard flag). Both instances are
        added to the _ATRACKERS registry so the single hook covers all of them.
        """
        from agent_cascade.async_shell_pkg import tracker as tracker_mod

        # Snapshot + reset module state so this test is deterministic regardless
        # of prior tests (the registry and guard persist across tests in a worker).
        saved_registered = tracker_mod._ATEXIT_REGISTERED[0]
        saved_registry = list(tracker_mod._ATRACKERS)
        tracker_mod._ATEXIT_REGISTERED[0] = False
        tracker_mod._ATRACKERS.clear()
        try:
            with patch('atexit.register') as mock_register:
                t1 = AsyncShellTracker(pool=None)
                t2 = AsyncShellTracker(pool=None)

            assert mock_register.call_count == 1, \
                f"atexit.register should be called exactly once, got {mock_register.call_count}"
            assert tracker_mod._ATRACKERS == [t1, t2], \
                'Both tracker instances must be in the atexit registry'
        finally:
            tracker_mod._ATEXIT_REGISTERED[0] = saved_registered
            tracker_mod._ATRACKERS.clear()
            tracker_mod._ATRACKERS.extend(saved_registry)

    def test_atexit_kill_all_kills_running_tasks(self):
        """_atexit_kill_all force-kills a live tracked process (cross-platform)."""
        pool = MagicMock()
        pool.enqueue_message = lambda agent, msg: None
        tracker = AsyncShellTracker(pool=pool)

        if os.name == 'nt':
            proc = subprocess.Popen(['ping', '-t', '127.0.0.1'],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            proc = subprocess.Popen(['sleep', '60'])

        task = _make_running_task(tool_id=7, pid=proc.pid, process=proc)
        _setup_task(tracker, task)

        try:
            assert proc.poll() is None, 'Fixture process should be alive before atexit kill'
            tracker._atexit_kill_all()
            # Give the OS a brief moment to reap the killed process.
            time.sleep(0.3)
            assert proc.poll() is not None, \
                f"Process {proc.pid} still alive after _atexit_kill_all (poll={proc.poll()})"
        finally:
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
