"""Tests for async shell_cmd kill mechanism.

Verifies that __kill actually terminates processes and returns only after
confirmation, fixing bugs where kill returned but heartbeats continued.
"""

import os
import sys
import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agent_cascade.async_shell import AsyncShellTracker, AsyncShellTask, KILL_WAIT_TIMEOUT


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
            result = tracker.kill_task('test_agent', 1)

            mock_kill.assert_called_once()
            assert mock_kill.call_args[0][0] is proc_mock

    @pytest.mark.parametrize("poll_behavior,expected_in_result", [
        ("waits_then_dead", "Shell killed"),
        ("never_dies", "did not terminate"),
    ])
    def test_kill_returns_after_process_confirmed_dead(self, poll_behavior, expected_in_result):
        """kill_task waits until proc.poll() != None before returning success (or errors on timeout)."""
        tracker = AsyncShellTracker(pool=None)
        proc_mock = MagicMock()
        proc_mock.pid = 99999

        if poll_behavior == "waits_then_dead":
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

        elif poll_behavior == "never_dies":
            proc_mock.poll.return_value = None  # Never dies

            task = _make_running_task(pid=99999, process=proc_mock)
            _setup_task(tracker, task)

            with patch('agent_cascade.async_shell.KILL_WAIT_TIMEOUT', 0.3):
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

        tool_id, pid, _, completed_early, _ = tracker.launch(
            agent_name='test_agent',
            command=cmd,
            heartbeat_interval=0.5,
            timeout=3600,
        )

        assert not completed_early, "Command should not complete instantly"

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
                pytest.skip("tasklist timed out")

    def test_kill_returns_only_after_process_dead(self):
        """kill_task blocks until the process is confirmed terminated (Windows)."""
        if os.name != 'nt':
            pytest.skip("Windows-only test")

        pool = MagicMock()
        pool.enqueue_message = lambda agent, msg: None

        tracker = AsyncShellTracker(pool=pool)

        tool_id, _, _, completed_early, _ = tracker.launch(
            agent_name='test_agent',
            command='ping -t 127.0.0.1 >nul 2>&1',
            heartbeat_interval=-1,
            timeout=3600,
        )

        assert not completed_early

        # Wait for tracking thread to set the PID on the task
        time.sleep(0.5)

        # Get real PID from task (launch returns 0 since PID is set async)
        task = tracker._get_task('test_agent', tool_id)
        pid = task.pid if task else None
        assert pid is not None and pid > 0, f"PID not set yet: {pid}"

        # Record time before kill
        start = time.time()
        result = tracker.kill_task('test_agent', tool_id)
        elapsed = time.time() - start

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
            pytest.skip("tasklist timed out")


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
            pytest.skip("Windows-only feature")

        pool = MagicMock()
        pool.enqueue_message = lambda agent, msg: None

        tracker = AsyncShellTracker(pool=pool)

        # Mock subprocess.Popen to capture the actual command used
        captured_cmd = []

        original_popen = subprocess.Popen

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

        assert captured_cmd, "No command was captured"
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


if __name__ == '__main__':
    pytest.main([__file__, '-v'])